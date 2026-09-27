"""信号组合模拟净值（方案 3）——「跟着买」的钱包曲线回放。

回答的问题
----------
体检（strategy_eval.py）回答「信号有没有预测力」；本模块回答更实在的
一句：「**如果真的照着买，钱包会怎样？**」

规则（写死、透明，不做任何隐藏参数）
--------------------------------
1. 信号日 T 收盘后得到命中集合（复用体检的命中序列，见 strategy_eval
   的 _HITS_CACHE——因此必须先跑一次同参数的体检）。
2. **次日开盘等权买入**。当日收盘才看到的信号，次日才能执行，杜绝
   「用今天的信息买今天的股票」的未来函数。
3. 次日**开盘涨停（含一字板）买不进**，跳过：开盘价 ≥ 昨收 × 涨停比例
   即视为买不到（近似口径，见 limit_ratio 注释）。
4. 持有 N 个交易日后（按该股自己的交易日历，停牌自然顺延）在
   **收盘价卖出**。
5. 费率：双边合计 15bp（佣金约万 2.5×2 + 卖出印花税 5bp + 杂费），
   买入腿 7.5bp、卖出腿 7.5bp。
6. 组合每日收益 = 当日活跃持仓个股收益的**等权平均**；空仓日为 0
   （现金趴账）。
7. 数据尾部仍未平仓的交易，按最后可得收盘价强平（计入 n_open），
   保证净值曲线连续到最后。

为什么基准是「全市场等权」而不是沪深 300
----------------------------------------
库里只有个股日线、没有指数日线。等权基准 = 每天全市场所有股票当日
收益的平均，代表「闭着眼随机买」的平均水平——对短线选股策略而言，
这比大盘加权指数（被权重股绑架）更公平：策略命中集合也是等权的。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

#: 默认双边费率（基点）。佣金万2.5×2 + 印花税万5 + 过户杂费 ≈ 15bp
DEFAULT_FEE_BPS = 15.0


# ---------------------------------------------------------------------------
# 涨停比例（近似口径）
# ---------------------------------------------------------------------------

def limit_ratio(code: str) -> float:
    """按板块返回「昨收 × 此值 ≈ 涨停价」的比例。

    库里没有 ST 标记与板块元数据，只能按代码前缀近似：
        sh68*  科创板   20%
        sz30*  创业板   20%（300/301/302 均已注册制 20cm）
        bj*    北交所   30%
        其余    沪深主板 10%
    注意：ST 股实际是 5%，会被本口径漏判为「可买」——这只会让模拟
    略偏乐观，且主板涨停判定本就偏保守（高开 ≥9.8% 的一律不买）。
    """
    if code.startswith("sh68"):
        return 1.198
    if code.startswith("sz30"):
        return 1.198
    if code.startswith("bj"):
        return 1.298
    return 1.098


def _is_limit_up_open(open_px: float, prev_close: float, code: str) -> bool:
    """次日开盘是否视为「买不进」（开盘即涨停）。"""
    if open_px <= 0 or prev_close <= 0:
        return False
    return open_px >= prev_close * limit_ratio(code) - 1e-9


# ---------------------------------------------------------------------------
# 交易生成（纯函数，便于离线自检）
# ---------------------------------------------------------------------------

def _build_trades(
    hit_days: Dict[str, set],
    eval_days: List[str],
    eng: Any,
    hold: int,
    fee_b: float,
    fee_s: float,
) -> Tuple[List[Dict[str, Any]], int]:
    """把「信号 → 交易」展开。返回 (trades, limit_skipped)。

    每笔 trade：
      legs: [(date, ret), ...]   逐 bar 收益率（已含该腿费用）
      closed: bool               是否在数据内正常平仓
    """
    trades: List[Dict[str, Any]] = []
    limit_skipped = 0

    for dt in eval_days:
        for code in hit_days.get(dt, ()):  # 信号日 T 的命中股
            s = eng._data.get(code)
            if not s:
                continue
            dates = s["dates"]
            i = bisect_left(dates, dt)
            if i >= len(dates) or dates[i] != dt:
                continue                      # 信号日无 bar（理论不发生）
            i_entry = i + 1                   # 次日（该股日历上的下一根 bar）
            if i_entry >= len(dates):
                continue                      # 数据尾部无次日 → 无法执行
            prev_close = float(s["close"][i])
            open_px = float(s["open"][i_entry])
            if open_px <= 0 or prev_close <= 0:
                continue
            if _is_limit_up_open(open_px, prev_close, code):
                limit_skipped += 1
                continue

            # 卖出日：买入日起第 hold 根 bar。数据尾部截断 → 最后一天强平
            i_exit = i_entry + hold - 1
            i_last = len(dates) - 1
            exit_at = min(i_exit, i_last)
            closed = i_exit <= i_last
            close = s["close"]

            legs: List[Tuple[str, float]] = []
            for j in range(i_entry, exit_at + 1):
                c = float(close[j])
                if j == i_entry:                       # 买入日：开盘价 + 买入费
                    base = open_px * (1.0 + fee_b)
                else:
                    base = float(close[j - 1])         # 中间日：昨收
                if j == exit_at:                       # 卖出日：卖出费
                    legs.append((dates[j], c * (1.0 - fee_s) / base - 1.0))
                else:
                    legs.append((dates[j], c / base - 1.0))

            trades.append({
                "code": code, "signal": dt,
                "legs": legs, "closed": closed,
                "bars": len(legs),
                # 逐笔总收益（含双边费用），用于胜率统计
                "ret": float(np.prod([1.0 + r for _, r in legs]) - 1.0)
                       if legs else 0.0,
            })
    return trades, limit_skipped


def _compose(
    trades: List[Dict[str, Any]],
    axis: List[str],
    start_date: str,
    bench_rets: Dict[str, float],
) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    """把逐笔交易的 legs 聚合成组合净值曲线。纯函数。

    axis        全局交易日轴（已排序）
    start_date  曲线起点（第一个信号日）
    bench_rets  全市场等权日收益 {date: ret}
    """
    daily: Dict[str, List[float]] = {}
    for t in trades:
        for d, r in t["legs"]:
            daily.setdefault(d, []).append(r)

    curve: List[Dict[str, float]] = []
    nav = 1.0
    bnav = 1.0
    started = False
    for dt in axis:
        if dt < start_date:
            continue
        started = True
        rs = daily.get(dt)
        r = float(np.mean(rs)) if rs else 0.0
        br = bench_rets.get(dt, 0.0)
        nav *= (1.0 + r)
        bnav *= (1.0 + br)
        curve.append({"date": dt, "nav": round(nav, 6),
                      "bench": round(bnav, 6)})
    if not started:      # 起点不在轴上（理论不发生）——退化为空曲线
        return [], {}

    navs = np.array([c["nav"] for c in curve], dtype=np.float64)
    peak = np.maximum.accumulate(navs)
    max_dd = float((navs / peak - 1.0).min()) if len(navs) else 0.0

    # ---- 回测质量指标（蒸馏自 cinar/indicator 的 sharpe_ratio / sortino）----
    # 日收益序列来自净值曲线相邻两天；std≈0（直线）时 Sharpe/Sortino 退化为
    # None，而不是 0/0 → 避免把「无波动直线」误判为超额收益。
    dret = navs[1:] / navs[:-1] - 1.0 if len(navs) > 1 else np.array([])
    mean_d = float(dret.mean()) if len(dret) else 0.0
    sd = float(dret.std(ddof=1)) if len(dret) > 1 else 0.0
    sharpe = (mean_d / sd * math.sqrt(250.0)
              if sd > 1e-12 and not math.isnan(sd) else None)
    downside = dret[dret < 0]
    ds = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = (mean_d / ds * math.sqrt(250.0)
               if ds > 1e-12 and not math.isnan(ds) else None)
    # 年化收益（基于总收益与窗口长度复利外推）
    n_days = len(curve)
    annual = (round((1.0 + (navs[-1] - 1.0)) ** (250.0 / n_days) - 1.0, 4)
              if n_days > 0 and navs[-1] > 0.0001 else None)
    calmar = (round(annual / abs(max_dd), 4)
              if (annual is not None and max_dd < 0) else None)
    # 盈亏比 = 平均盈利 / 平均亏损（逐笔口径）
    wins = [t["ret"] for t in trades if t["ret"] > 0]
    losses = [t["ret"] for t in trades if t["ret"] < 0]
    pl_ratio = (round(float(np.mean(wins)) / abs(float(np.mean(losses))), 4)
                if wins and losses else None)

    closed_rets = [t["ret"] for t in trades]
    win_rate = (float(np.mean([1.0 if r > 0 else 0.0 for r in closed_rets]))
                if closed_rets else None)
    stats = {
        "n_days": n_days,
        "total_ret": round(navs[-1] - 1.0, 4) if len(navs) else 0.0,
        "bench_ret": round(bnav - 1.0, 4) if curve else 0.0,
        "max_dd": round(max_dd, 4),
        "annual_ret": annual,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "profit_loss_ratio": pl_ratio,
        "win_rate": (round(win_rate, 4) if win_rate is not None else None),
        "n_trades": len(trades),
        "n_open": sum(1 for t in trades if not t["closed"]),
        "avg_bars": (round(float(np.mean([t["bars"] for t in trades])), 1)
                     if trades else 0.0),
    }
    return curve, stats


def _market_bench(eng: Any) -> Dict[str, float]:
    """全市场等权日收益 {date: ret}。

    每只股票相邻两根 bar 的收益（停牌段自然并到复牌日），再按日期对
    全市场求平均。只碰内存 ndarray。

    关键：用 np.bincount 做「按日期分桶累加」，替代原先的 np.add.at
    （np.add.at 是未向量化的缓冲散点写，全市场 ~3700 只 × ~500 日会让
    首次模拟的基准曲线计算卡 30s+）。bincount 是同语义的向量化实现，
    首次计算也 < 1 秒。
    """
    # 全局日期轴（所有股票出现过的日期并集，量级 ~500，开销可忽略）
    all_dates = set()
    for s in eng._data.values():
        all_dates.update(s["dates"])
    i2d = sorted(all_dates)
    date2i = {d: i for i, d in enumerate(i2d)}

    n = len(i2d)
    bsum = np.zeros(n, dtype=np.float64)
    bcnt = np.zeros(n, dtype=np.int64)
    for s in eng._data.values():
        C = s["close"]
        if len(C) < 2:
            continue
        rets = (C[1:].astype(np.float64) / C[:-1].astype(np.float64)) - 1.0
        gi = np.fromiter((date2i[d] for d in s["dates"][1:]),
                         dtype=np.int64, count=len(rets))
        bsum += np.bincount(gi, weights=rets, minlength=n)
        bcnt += np.bincount(gi, minlength=n)
    out: Dict[str, float] = {}
    for i in range(n):
        if bcnt[i] > 0:
            out[i2d[i]] = float(bsum[i] / bcnt[i])
    return out


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def simulate(key: str, days: int = 120, hold: int = 5,
             fee_bps: float = DEFAULT_FEE_BPS) -> Dict[str, Any]:
    """对某个策略的命中集合做「跟着买」回放。

    前置条件：同参数（days）的体检已经跑过（缓存内），否则返回
    need_eval=True 让前端引导用户先做体检——**绝不在这里悄悄触发
    一次 40 秒的重算**（同步请求会被网关 504 掐断）。
    """
    import history
    import strategy_eval as se
    import screener

    d = screener.STRATEGY_BY_KEY.get(key)
    if not d:
        return {"ok": False, "error": f"未知策略：{key}"}

    eng = history.get_engine(auto_load=False)
    if not eng or not eng._data:
        return {"ok": False, "error": "历史数据未加载"}

    if not se.hits_ready(days, forward=5):
        return {"ok": False, "need_eval": True,
                "error": "还没有该回看天数的体检结果，请先执行一次策略体检"}

    hc = se.get_hits()
    eval_days = hc.get("eval_days") or []
    hit_days = (hc.get("hits") or {}).get(key) or {}
    if not eval_days or not hit_days:
        return {"ok": False, "need_eval": True,
                "error": "体检缓存里没有命中序列，请重新执行一次策略体检"}

    fee = fee_bps / 10000.0
    fee_b = fee / 2.0
    fee_s = fee / 2.0

    trades, limit_skipped = _build_trades(hit_days, eval_days, eng,
                                          hold, fee_b, fee_s)
    if not trades:
        return {"ok": False,
                "error": "该策略在窗口内没有可执行交易（全部涨停跳过或数据不足）"}

    axis = se._build_axis(eng)
    if not axis:
        return {"ok": False, "error": "交易日轴为空"}
    bench = _market_bench(eng)
    curve, stats = _compose(trades, axis, eval_days[0], bench)

    total = stats["total_ret"]

    return {
        "ok": True,
        "key": key,
        "name": d.get("name", key),
        "days": days,             # 信号窗口（与体检口径一致）
        "hold": hold,
        "fee_bps": fee_bps,
        "span": [eval_days[0], eval_days[-1]],
        "curve": curve,
        "stats": {**stats,
                  "limit_skipped": limit_skipped,
                  "excess": round(total - stats["bench_ret"], 4)},
        "as_of": hc.get("as_of", ""),
    }


# ---------------------------------------------------------------------------
# 自检（纯函数，不依赖真实数据）
# ---------------------------------------------------------------------------

def selfcheck() -> Dict[str, Any]:
    """离线自检：合成数据验证交易生成与净值聚合的正确性。"""
    steps: List[Dict[str, Any]] = []

    def rec(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail})

    # ---- 合成一个迷你引擎：1 只股票、已知价格路径 ----
    #        idx:  0     1     2     3     4     5
    #        C  : 10.0  11.0  12.0  9.0   10.0  11.0
    #        O  :  9.5  10.5  11.5  9.5   9.5   10.5
    #        dates: d0 .. d5
    class _FakeEng:
        _as_of = "d5"
        _data = {
            "TEST": {
                "close": np.array([10.0, 11.0, 12.0, 9.0, 10.0, 11.0],
                                  dtype=np.float32),
                "open": np.array([9.5, 10.5, 11.5, 9.5, 9.5, 10.5],
                                 dtype=np.float32),
                "high": np.full(6, 99.0, dtype=np.float32),
                "low": np.full(6, 1.0, dtype=np.float32),
                "volume": np.ones(6, dtype=np.float32),
                "amount": np.ones(6, dtype=np.float32),
                "dates": ["d0", "d1", "d2", "d3", "d4", "d5"],
            }
        }

    eng = _FakeEng()

    # 1. 无费用、hold=2、d0 信号：d1 开盘 10.5 买，d2 收盘 12 卖
    trades, skipped = _build_trades({"d0": {"TEST"}}, ["d0"], eng, 2, 0.0, 0.0)
    t = trades[0] if trades else None
    rec("信号次日开盘买入",
        t is not None and t["legs"][0] == ("d1", 11.0 / 10.5 - 1.0),
        str(t["legs"][:1]) if t else "无交易")
    rec("持有 2 日收盘卖出",
        t is not None and len(t["legs"]) == 2
        and t["legs"][1] == ("d2", 12.0 / 11.0 - 1.0),
        str(t["bars"]) if t else "—")
    rec("正常平仓 closed=True", t is not None and t["closed"])
    rec("无涨停跳过", skipped == 0)

    # 2. 费用：买入腿 7.5bp、卖出腿 7.5bp
    fb = 0.00075
    trades, _ = _build_trades({"d0": {"TEST"}}, ["d0"], eng, 2, fb, fb)
    t = trades[0]
    e0 = t["legs"][0][1]
    e1 = t["legs"][1][1]
    rec("买入腿含费", abs(e0 - (11.0 / (10.5 * (1 + fb)) - 1.0)) < 1e-12,
        f"{e0:.6f}")
    rec("卖出腿含费", abs(e1 - (12.0 * (1 - fb) / 11.0 - 1.0)) < 1e-12,
        f"{e1:.6f}")

    # 3. 次日开盘涨停 → 跳过。d2 收 12 → d3 开盘涨停比例 1.098×12=13.176
    eng._data["TEST"]["open"][3] = 13.2
    trades, skipped = _build_trades({"d2": {"TEST"}}, ["d2"], eng, 2, 0, 0)
    rec("开盘涨停买不进 → 跳过", len(trades) == 0 and skipped == 1,
        f"skipped={skipped}")
    eng._data["TEST"]["open"][3] = 9.5     # 还原

    # 4. 数据尾部未平仓 → 强平且 n_open 计数
    trades, _ = _build_trades({"d5": {"TEST"}}, ["d5"], eng, 2, 0, 0)
    t = trades[0] if trades else None
    rec("尾部信号无法买入（无次日）", t is None,
        "d5 是最后一根，没有次一交易日")

    # 5. 净值聚合：两笔已知收益串联 + 空仓日走平
    fake_trades = [
        {"code": "A", "signal": "d0", "closed": True, "bars": 2,
         "ret": 0.15,
         "legs": [("d1", 0.10), ("d2", 0.05)]},
        {"code": "B", "signal": "d0", "closed": True, "bars": 2,
         "ret": -0.05,
         "legs": [("d1", -0.10), ("d2", 0.05)]},
    ]
    axis = ["d0", "d1", "d2", "d3"]
    bench = {"d1": 0.01, "d2": -0.02, "d3": 0.005}
    curve, stats = _compose(fake_trades, axis, "d0", bench)
    # 曲线含信号日当天（d0，nav=1）；d1 组合收益 = (0.10 - 0.10)/2 = 0；
    # d2 = (0.05+0.05)/2 = 0.05
    rec("组合日收益 = 活跃交易均值",
        len(curve) == 4 and abs(curve[0]["nav"] - 1.0) < 1e-9
        and abs(curve[1]["nav"] - 1.0) < 1e-9
        and abs(curve[2]["nav"] - 1.05) < 1e-9,
        str([c["nav"] for c in curve]))
    rec("空仓日净值走平", abs(curve[3]["nav"] - 1.05) < 1e-9,
        f"d3={curve[3]['nav']}")
    rec("基准曲线独立累积",
        abs(curve[3]["bench"] - 1.01 * 0.98 * 1.005) < 1e-9,
        f"bench={curve[3]['bench']}")
    rec("最大回撤 = 0（一路上坡）", stats["max_dd"] == 0.0,
        f"dd={stats['max_dd']}")

    # 6. 最大回撤计算（构造先涨后跌）
    fake_trades = [
        {"code": "A", "signal": "d0", "closed": True, "bars": 3,
         "ret": -0.3,
         "legs": [("d1", 0.50), ("d2", -0.40), ("d3", 0.0)]},
    ]
    curve, stats = _compose(fake_trades, ["d0", "d1", "d2", "d3"], "d0", {})
    # nav: 1 → 1.5 → 0.9 → 0.9 ；回撤 = 0.9/1.5 - 1 = -0.4
    rec("最大回撤 = -40%",
        abs(stats["max_dd"] - (-0.4)) < 1e-9, f"dd={stats['max_dd']}")

    # 7. 胜率
    fake_trades = [
        {"code": "A", "signal": "d0", "closed": True, "bars": 1,
         "ret": 0.1, "legs": [("d1", 0.1)]},
        {"code": "B", "signal": "d0", "closed": True, "bars": 1,
         "ret": -0.1, "legs": [("d1", -0.1)]},
        {"code": "C", "signal": "d0", "closed": True, "bars": 1,
         "ret": 0.0, "legs": [("d1", 0.0)]},
    ]
    curve, stats = _compose(fake_trades, ["d0", "d1"], "d0", {})
    rec("胜率只算 >0（1/3）", stats["win_rate"] is not None
        and abs(stats["win_rate"] - 0.3333) < 1e-6,
        f"win={stats['win_rate']}")

    # 9. 回测质量指标：Sharpe / Sortino / Calmar / 盈亏比（蒸馏自 indicator 库）
    # 直线上涨（每日 +1%）→ 无波动 → std≈0 → Sharpe/Sortino 退化为 None，
    # 而不是 0/0（避免把无波动直线误判为超额收益）
    lin = [{"code": "A", "signal": "d0", "closed": True, "bars": 1, "ret": 0.0,
            "legs": [("d1", 0.01), ("d2", 0.01)]},
           {"code": "B", "signal": "d0", "closed": True, "bars": 1, "ret": 0.0,
            "legs": [("d1", 0.01), ("d2", 0.01)]}]
    curve, stats = _compose(lin, ["d0", "d1", "d2"], "d0", {})
    rec("直线净值 Sharpe/Sortino 退化为 None",
        stats["sharpe"] is None and stats["sortino"] is None,
        f"sharpe={stats['sharpe']} sortino={stats['sortino']}")

    # 对称振荡（每日 +2%/-2% 交替）→ 均值≈0 → Sharpe≈0，但 downside 有样本
    # → Sortino 非 None（验证下行波动口径）；Calmar 因年化≈0 也非 None
    osc_legs = [("d%d" % i, 0.02 if i % 2 == 0 else -0.02) for i in range(2, 12)]
    osc = [{"code": "A", "signal": "d0", "closed": True, "bars": 10, "ret": 0.0,
            "legs": osc_legs}]
    curve, stats = _compose(osc, ["d0"] + ["d%d" % i for i in range(2, 12)], "d0", {})
    rec("振荡净值 Sharpe 非 None",
        stats["sharpe"] is not None and abs(stats["sharpe"]) < 1.0,
        f"sharpe={stats['sharpe']}")
    rec("振荡净值 Sortino 非 None（down 有样本）",
        stats["sortino"] is not None, f"sortino={stats['sortino']}")
    rec("Calmar 非 None", stats["calmar"] is not None, f"calmar={stats['calmar']}")

    # 盈亏比：一盈一亏 → 0.10 / 0.05 = 2.0
    pl = [{"code": "A", "signal": "d0", "closed": True, "bars": 1, "ret": 0.10,
           "legs": [("d1", 0.10)]},
          {"code": "B", "signal": "d0", "closed": True, "bars": 1, "ret": -0.05,
           "legs": [("d1", -0.05)]}]
    curve, stats = _compose(pl, ["d0", "d1"], "d0", {})
    rec("盈亏比 = 2.0", stats["profit_loss_ratio"] is not None
        and abs(stats["profit_loss_ratio"] - 2.0) < 1e-6,
        f"pl={stats['profit_loss_ratio']}")

    # 8. limit_ratio 板块区分
    rec("主板 10cm", limit_ratio("sh600519") == 1.098)
    rec("科创板 20cm", limit_ratio("sh688001") == 1.198)
    rec("创业板 20cm", limit_ratio("sz300001") == 1.198)
    rec("北交所 30cm", limit_ratio("bj920001") == 1.298)

    passed = sum(1 for s in steps if s["ok"])
    return {
        "steps": steps,
        "total": len(steps),
        "passed": passed,
        "failed": len(steps) - passed,
        "all_passed": passed == len(steps),
    }


if __name__ == "__main__":
    r = selfcheck()
    for s in r["steps"]:
        print(f"  {'✅' if s['ok'] else '❌'} {s['name']}  {s['detail']}")
    print()
    print(f"{'✅ 全部通过' if r['all_passed'] else '❌ 有失败'}  "
          f"{r['passed']}/{r['total']}")
