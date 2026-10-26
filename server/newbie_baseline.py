"""随机基线 —— 回答"这个选股方案真的比随便选强吗"。

## 为什么要有这个模块

小白选股（`newbie.py`）给出 20 只票，用户无法判断"这 20 只到底是筛选出来的，
还是随便挑 20 只也差不多"。本模块用**历史回溯**回答这个问题：

    取某方案在历史上每个交易日的实际选股（或可复现的近似），
    与"同一天随机抽同样多只票"相比，未来 N 天收益谁高。

结论的表述形式是百分位：**"跑赢 X% 的随机选股"**。
这是本模块唯一需要对外承诺的数字。

## 与 TradingView-API 的关系

移植自其 `src/backtest/stats.ts` 的 `randomBaseline` / `percentileRank`
/ `quantile`，但做了一处**必要的改写**：

- 原版是"同一批合格 bar 上随机抽 entry，同退出同成本"——它跑在完整回测
  引擎上。我们的 newbie 是**当日快照筛选器**，没有 bar 级回测引擎。
- 所以这里改成"**同一交易日、同一股票池、同样只数**"的横截面随机抽样。
  这比 bar 级基线更容易理解和验证，也正好匹配 newbie 的实际形态。

数值约定与宏观口径保持一致：
- 调仓价用**信号日次日收盘**（`lag=1`，与 `factor_lab.forward_returns`
  同一口径），避免"用信号日收盘价买入"的前视。
- 随机抽样用 `numpy.random.default_rng(seed)`，同种子必然同结果。

## ponytail 备注

`ponytail:` 随机抽样是 O(runs × 天数 × 只数) 的纯 Python 循环。当前参数
（runs=200 × 60 天 × 20 只 ≈ 24 万次）实测在 1 秒内，够用。若将来把 runs
提到 5000 或天数提到 500，改成 numpy 向量化（一次抽 (runs, k) 的索引矩阵）。
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

DEFAULT_RUNS = 200          # 每次评估的随机抽样次数
DEFAULT_DAYS = 60           # 回溯多少个交易日
DEFAULT_HORIZON = 5         # 未来 N 天收益
DEFAULT_SEED = 42


# ---------------------------------------------------------------------------
# 统计基元
# ---------------------------------------------------------------------------

def quantile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    """升序数组的线性插值分位数（q ∈ [0,1]）。空数组返回 None。

    与 numpy.percentile(..., method='linear') 等价，但只依赖传入已排序数组，
    避免调用方反复排序。
    """
    n = len(sorted_values)
    if n == 0:
        return None
    pos = (n - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def percentile_rank(value: float, pool: Sequence[float]) -> Optional[float]:
    """value 在 pool 中的百分位：严格小于 value 的比例 ×100。

    用"严格小于"而不是"小于等于"，是为了让"和随机完全一样"落在 50 附近
    而不是虚高。pool 为空返回 None。
    """
    if not pool:
        return None
    return 100.0 * sum(1 for x in pool if x < value) / len(pool)


# ---------------------------------------------------------------------------
# 收益计算
# ---------------------------------------------------------------------------

def forward_return(closes: np.ndarray, t: int, horizon: int, lag: int = 1
                   ) -> Optional[float]:
    """第 t 日出的信号，未来 horizon 天的收益。

    口径与 `factor_lab.forward_returns` 一致：
        ret = close[t+lag+horizon] / close[t+lag] - 1
    lag=1 表示 t 日收盘出信号、t+1 日收盘买入。

    数据不足（越界）或价格非正时返回 None —— 由调用方决定是丢弃还是记 0，
    绝不能静默当 0 收益，那会把"没数据"算成"平盘"。
    """
    i = t + lag
    j = i + horizon
    if i < 0 or j >= len(closes):
        return None
    a, b = float(closes[i]), float(closes[j])
    if not (a > 0 and b > 0):
        return None
    return b / a - 1.0


def mean_return(rets: Sequence[Optional[float]], min_valid: int = 1
                ) -> Optional[float]:
    """一组收益的等权均值；有效样本不足 min_valid 时返回 None。"""
    v = [r for r in rets if r is not None and math.isfinite(r)]
    if len(v) < min_valid:
        return None
    return sum(v) / len(v)


# ---------------------------------------------------------------------------
# 随机基线
# ---------------------------------------------------------------------------

def random_baseline(candidates: Sequence[str],
                    picks: Sequence[str],
                    returns: Dict[str, Sequence[Optional[float]]],
                    runs: int = DEFAULT_RUNS,
                    seed: int = DEFAULT_SEED,
                    matched_pool: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """同一交易日、同一股票池下的随机选股基线。

    参数：
        candidates: 当日合格股票池（code 列表）。必须与 picks 同源，
                    否则比较的不是"同样条件下随机 vs 策略"。
        picks:      策略当日实际选出的股票（code 列表）。
        returns:    code -> 该日未来收益（None 表示无数据）。
        runs:       随机抽样次数。
        seed:       随机种子，同种子结果可复现。
        matched_pool: 可选，"同等条件子池"。传了就额外算一份 matched 口径。

    返回：
        {
          "runs": 实际有效抽样次数,
          "strategy": 策略当日等权收益（无有效收益时为 None）,
          "random": [每次随机的等权收益...],
          "percentile": 策略收益在随机分布中的百分位,
          "p5"/"p50"/"p95": 随机分布的三个分位点,
          "matched": {...同结构，仅当传了 matched_pool...},
          "note": 不可比时的说明（None 表示可比）
        }

    ## 为什么有两个口径（这是本模块最重要的设计决定）

    整池随机（`percentile`）回答：**相对随便买，策略有没有更好？**
    同强势子池（`matched.percentile`）回答：**在同等强度里，精细打分有没有额外价值？**

    实测（growth 预设，2025-11~2026-04）：整池 39.4%、同强势子池 50.9%。
    两个数字差了 11 个百分点，指向完全不同的结论：
      - 只报整池 → "追涨还不如随便买"
      - 只报子池 → "打分没用"
    两条都是事实，但单独看都会误导。所以**必须同时给**，缺一个就是选择性呈现。

    可比性守则（重要）：
        - 池子 < 选股数 → 无法抽够，note 说明，percentile 为 None
        - 策略收益为 None（数据缺失）→ 该日整次跳过，不参与统计
        - 任一情况都不允许"补零"蒙混过关
    """
    k = len(picks)
    strat = mean_return([returns.get(c) for c in picks])
    out: Dict[str, Any] = {
        "runs": 0, "strategy": strat, "random": [],
        "percentile": None, "p5": None, "p50": None, "p95": None,
        "matched": None, "note": None,
    }
    if strat is None:
        out["note"] = "策略选股在该日无有效收益数据"
        return out
    if k == 0:
        out["note"] = "策略当日无选股"
        return out

    def _one(pool: Sequence[str], tag: str) -> Optional[Dict[str, Any]]:
        """在 pool 里抽 k 只跑 runs 次；不可比返回 None 并写 note。"""
        pool = list(pool)
        # 注意边界：pool == k 是**可比**的（随机只能抽出唯一组合，退化为
        # 策略自身收益，百分位 50）。只有 pool < k 才抽不够。
        if len(pool) < k:
            return None
        valid = [c for c in pool if returns.get(c) is not None]
        if len(valid) < k:
            return None
        rng = np.random.default_rng(seed)
        # ponytail: 纯 Python 循环，24 万次调用约 1 秒内；见模块 docstring
        rand_rets: List[float] = []
        for _ in range(int(runs)):
            idx = rng.choice(len(valid), size=k, replace=False)
            r = mean_return([returns.get(valid[i]) for i in idx])
            if r is not None:
                rand_rets.append(r)
        if not rand_rets:
            return None
        s = sorted(rand_rets)
        # 用中点口径 (below + 0.5×equal)/n：并列（随机与策略同收益）算"打平"，
        # 不给任何一方加分。严格小于口径会把"完全一样"算成 0 分，与直觉相反，
        # 而 50 分才能表达"无差别"。
        below = sum(1 for x in rand_rets if x < strat)
        equal = sum(1 for x in rand_rets if x == strat)
        return {
            "runs": len(rand_rets),
            "random": rand_rets,
            "percentile": 100.0 * (below + 0.5 * equal) / len(rand_rets),
            "p5": quantile(s, 0.05),
            "p50": quantile(s, 0.50),
            "p95": quantile(s, 0.95),
        }

    base = _one(candidates, "all")
    if base is None:
        out["note"] = (f"股票池({len(candidates)})/有效收益样本不足，无法随机对比"
                       f"（选股 {k} 只）")
        return out
    out.update(base)
    out["matched"] = _one(matched_pool, "matched") if matched_pool else None
    return out


def aggregate(per_day: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐日基线结果汇总成双口径结论。

    两个口径分别汇总，互不替代（理由见 `random_baseline` 的 docstring）：
      - `vs_all`    : 整池随机 —— 相对随便买，策略有没有更好
      - `vs_matched`: 同强势子池 —— 同等强度里，精细打分有没有额外价值

    不可比的天数必须显式暴露（`days_skipped`），否则用户会以为
    百分位建立在全部天数上。
    """
    def _agg(scope: str) -> Dict[str, Any]:
        """scope: "all" 取顶层字段，"matched" 取 matched 子对象。"""
        def node(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            return d if scope == "all" else d.get("matched")

        usable = [d for d in per_day
                  if (node(d) or {}).get("percentile") is not None]
        if not usable:
            return {"days": 0, "percentile_mean": None, "win_rate": None,
                    "p5": None, "p50": None, "p95": None, "verdict": "样本不足，无法给出对比结论"}
        pcts = [node(d)["percentile"] for d in usable]
        rand_means = []
        for d in usable:
            rs = node(d).get("random") or []
            if rs:
                rand_means.append(sum(rs) / len(rs))
        mean_pct = sum(pcts) / len(pcts)
        win = sum(1 for p in pcts if p > 50) / len(pcts)
        sm = sorted(rand_means)
        return {
            "days": len(usable),
            "percentile_mean": mean_pct,
            "win_rate": win,
            "p5": quantile(sm, 0.05) if sm else None,
            "p50": quantile(sm, 0.50) if sm else None,
            "p95": quantile(sm, 0.95) if sm else None,
            "verdict": _verdict(mean_pct),
        }

    all_usable = [d for d in per_day if d.get("percentile") is not None]
    strat = [d["strategy"] for d in all_usable if d.get("strategy") is not None]
    return {
        "days_total": len(per_day),
        "days_usable": len(all_usable),
        "days_skipped": len(per_day) - len(all_usable),
        "strategy_mean": (sum(strat) / len(strat)) if strat else None,
        "vs_all": _agg("all"),
        "vs_matched": _agg("matched"),
    }


def _verdict(mean_pct: Optional[float]) -> str:
    """把百分位均值翻成一句人话。阈值是约定，不是统计检验。"""
    if mean_pct is None:
        return "样本不足，无法给出对比结论"
    if mean_pct >= 90:
        return "显著强于随机选股"
    if mean_pct >= 70:
        return "强于随机选股"
    if mean_pct >= 55:
        return "略强于随机选股"
    if mean_pct > 45:
        return "与随机选股无实质差别"
    return "弱于随机选股"


# ---------------------------------------------------------------------------
# 历史回溯：把日线矩阵接到基线上
# ---------------------------------------------------------------------------

def build_returns(closes_by_code: Dict[str, Sequence[Tuple[Any, ...]]],
                  horizon: int = DEFAULT_HORIZON,
                  lag: int = 1) -> Dict[str, Any]:
    """把 "code -> [(date, close[, amount])...]" 转成对齐矩阵。

    返回 {"dates", "codes", "close", "amount", "ret", "horizon", "lag"}。
    收益口径 ret[t] = close[t+lag+horizon]/close[t+lag] - 1，
    与 `factor_lab.forward_returns` 一致（lag=1 = t 日出信号、t+1 买入）。

    缺失价格一律 NaN，绝不用 0 填充——0 收益在统计上等价于"平盘"，
    会把"停牌/无数据"伪装成一个真实观测。
    """
    codes = sorted(closes_by_code)
    dates = sorted({r[0] for v in closes_by_code.values() for r in v})
    didx = {d: i for i, d in enumerate(dates)}
    T, N = len(dates), len(codes)

    close = np.full((T, N), np.nan)
    amount = np.full((T, N), np.nan)
    for j, code in enumerate(codes):
        for row in closes_by_code[code]:
            i = didx.get(row[0])
            if i is None:
                continue
            c = row[1]
            if c is not None and math.isfinite(float(c)) and float(c) > 0:
                close[i, j] = float(c)
            if len(row) > 2 and row[2] is not None:
                amount[i, j] = float(row[2])

    ret = np.full((T, N), np.nan)
    span = lag + horizon
    valid_end = -1
    if T > span:
        with np.errstate(divide="ignore", invalid="ignore"):
            r = close[span:] / close[lag:T - horizon] - 1.0
        r[~np.isfinite(r)] = np.nan
        ret[:T - span] = r
        valid_end = T - span - 1
    return {"dates": dates, "codes": codes, "close": close,
            "amount": amount, "ret": ret, "horizon": horizon, "lag": lag,
            "valid_end": valid_end}


def evaluate_preset(px: Dict[str, Any],
                    picker: Callable[[int, Dict[str, Any]], Tuple[List[int], List[int], List[int]]],
                    days: int = DEFAULT_DAYS,
                    runs: int = DEFAULT_RUNS,
                    seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """在历史窗口上逐日跑"策略 vs 随机"，返回可直接上屏的结论。

    `picker(t, ctx)` 必须返回 **三个** 列索引列表：
        `(picks, candidates, matched_pool)`
        - picks：策略当日选中的股票
        - candidates：当日合格池（"随便买"的抽样域）
        - matched_pool：同等条件子池（"同样强势里再挑"的抽样域），可为空

    `ctx` 提供 {"close", "amount", "codes", "dates"}，**只含 t 及之前的数据切片**，
    调用方不得越界读 t+1 及以后——这是前视防护的唯一执行点。

    ## 为什么 picker 是回调

    newbie 的四个预设各依赖不同字段：`steady` 用 pe/pb/total_cap，`growth` 用
    turnover/total_cap，`hot` 用涨跌停判定，`rebound` 用真实 K 线形态。而历史库
    `daily_bars` 只有 open/close/high/low/volume/amount，**turnover 与市值都没有**。
    与其在模块里内置一份"看起来像但字段不全"的降级策略（那是在骗人），
    不如把决策权交给调用方，本模块只负责基线与统计。
    """
    ret = px["ret"]
    T, N = ret.shape
    ctx = {"close": px["close"], "amount": px["amount"],
           "codes": px["codes"], "dates": px["dates"]}

    # 收益已到期的最后一天，直接取 `build_returns` 算好的 `valid_end`。
    # **不要在这里重算**：曾经写成 `T - lag - horizon - 1`，看着和
    # `build_returns` 里的 `T - span` 等价，实跑却丢掉最新 6 天（真库
    # 98 行里 92~97 全是 NaN）。两处口径本来就该由一处推导，重算就是等着错位。
    # 同一条链上还有一层保护：停牌股两边价格都是 NaN → 比值 NaN → 置 NaN，
    # 不会被当成"平盘 0%"混进统计。
    last = px.get("valid_end")
    if last is None or last < 0:
        last = T - px["lag"] - px["horizon"]
    first = max(0, last - days + 1)
    idxs = list(range(first, last + 1))

    per_day: List[Dict[str, Any]] = []
    for t in idxs:
        picks_i, cand_i, matched_i = picker(t, ctx)
        cds = px["codes"]
        if not picks_i:
            per_day.append({"date": px["dates"][t], "percentile": None,
                            "strategy": None, "random": [], "matched": None,
                            "note": "策略当日无选股"})
            continue
        rmap = {cds[j]: (None if not math.isfinite(ret[t, j]) else float(ret[t, j]))
                for j in cand_i}
        d = random_baseline([cds[j] for j in cand_i],
                            [cds[j] for j in picks_i],
                            rmap, runs=runs, seed=seed,
                            matched_pool=[cds[j] for j in matched_i] if matched_i else None)
        d["date"] = px["dates"][t]
        d["pool"] = len(cand_i)
        per_day.append(d)

    agg = aggregate(per_day)
    agg["window"] = {
        "from": px["dates"][idxs[0]] if idxs else None,
        "to": px["dates"][idxs[-1]] if idxs else None,
        "horizon": px["horizon"], "lag": px["lag"], "runs": runs,
    }
    # 逐日明细：前端画"每日百分位"小图要用。
    # 只留每天的两个百分位与当日池子规模 —— 不去传每天 200 次抽样的原始
    # 随机收益（那会把响应撑到几百 KB，而画图用不到）。
    agg["daily"] = [
        {"date": d.get("date"),
         "pct": d.get("percentile"),
         "pct_matched": ((d.get("matched") or {}).get("percentile")),
         "strat": d.get("strategy"),
         "pool": d.get("pool"),
         "note": d.get("note")}
        for d in per_day
    ]
    return agg


# ---------------------------------------------------------------------------
# 数据装载 + 预设适配（唯一碰数据库和 newbie 的地方）
# ---------------------------------------------------------------------------

# 各预设能否被"可信回溯"。key 为预设 key，值为 (能否回溯, 原因)。
# 这份表是**对外承诺**：前端据此决定显示数字还是显示"该方案无法回溯"。
RETROSPECT: Dict[str, Tuple[bool, str]] = {
    "growth": (True,
               "按当日涨幅排序取前 20，近似原方案（原方案另用 turnover/市值做筛选，"
               "历史库无此字段，故未纳入）"),
    "steady": (False,
               "原方案核心是 PE/PB/市值，历史 daily_bars 无这三个字段，"
               "强行回溯只会得到一个与真实策略无关的数字"),
    "hot": (False,
            "需要按板块判定涨跌停价与封板状态，历史库无板块涨停价，无法还原"),
    "rebound": (False,
                "需要逐只拉 70 根真实 K 线做形态判定，与回溯窗口叠加后无法在"
                "可接受时间内完成（实测吞吐约 2.5 只/秒）"),
}


def load_close_matrix(start: str, end: str) -> Dict[str, Any]:
    """从库里装载 [start, end] 的日线收盘价与成交额，组装成矩阵。

    一次窄窗口查询（约 6 个月 / 86 万行 / 2.4 秒），不做逐只查询——
    逐只 `store.get_bars` 是 0.14 秒/只 × 5000 只 ≈ 12 分钟，不可用。
    """
    from collections import defaultdict
    import store

    rows = store._conn().execute(
        "SELECT code,date,close,amount FROM daily_bars WHERE date>=? AND date<=?",
        (start, end)).fetchall()
    tmp: Dict[str, List[Tuple[Any, ...]]] = defaultdict(list)
    for code, d, c, a in rows:
        if c is not None and c > 0:
            tmp[code].append((d, c, a))
    return tmp


def growth_picker_factory(px: Dict[str, Any], limit_pct: float = 10.0,
                          top_n: int = 20, min_amount: float = 15000.0):
    """构造 growth 预设的可回溯近似 picker。

    与原 `_score_growth` 的差异（**必须如实标注**）：
      - 保留：涨幅 > 0、未封板（chg < 涨停-0.5）、成交额门槛
      - 丢弃：turnover（历史库无）、总市值区间（历史库无）
      - 排序：原方案是复合打分（涨幅 34 + 换手 26 + 成交额 22 + 小盘 18），
        这里只用涨幅排——因为只有它是可回溯的。
    所以这个数字衡量的是"**当日涨幅排序**"这一核心因子的价值，
    不是原方案完整打分的价值。前端必须把这句话展示出来。
    """
    close = px["close"]
    T, N = close.shape
    chg = np.full((T, N), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        chg[1:] = (close[1:] / close[:-1] - 1.0) * 100.0
    chg[~np.isfinite(chg)] = np.nan

    def picker(t: int, ctx: Dict[str, Any]):
        c = chg[t]
        amt = ctx["amount"][t]
        ok = (np.isfinite(c) & (c > 0) & (c < limit_pct - 0.5)
              & np.isfinite(amt) & (amt >= min_amount))
        pool = np.where(ok)[0]
        if len(pool) < top_n:
            return [], [], []
        order = pool[np.argsort(-c[pool])]
        picks = order[:top_n]
        cut = c[order[top_n - 1]]
        matched = pool[c[pool] >= cut]
        return list(picks), list(pool), list(matched)

    return picker


def _latest_bar_date() -> str:
    """库里最新一根日线的日期（YYYY-MM-DD）；空库返回 ""。"""
    import store
    row = store._conn().execute("SELECT MAX(date) FROM daily_bars").fetchone()
    return (row[0] or "") if row else ""


def evaluate_growth(start: str = "", end: str = "",
                    days: int = DEFAULT_DAYS, horizon: int = DEFAULT_HORIZON,
                    runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """面向 API 的一站式入口：装数据 → 建矩阵 → 跑基线。

    start/end 留空时 `end` 取**库里最新的日线日期**，而不是今天：
    非交易日、节假日、或当日增量还没入库时，用今天会自动往前挪掉一整个
    有效窗口（实测今天 10-26、库里最新 10-23，白丢 3 个自然日）。
    start 再按 days+horizon+lag 反推，按每周 5 个交易日估算并留 7 天余量。
    """
    import datetime as _dt

    if not end:
        end = _latest_bar_date()
        if not end:
            return {"ok": False, "reason": "库里没有任何日线数据，无法回溯"}
    if not start:
        need = days + horizon + 2 + 10
        start = (_dt.date.fromisoformat(end)
                 - _dt.timedelta(days=int(need / 5 * 7) + 7)).isoformat()

    tmp = load_close_matrix(start, end)
    if not tmp:
        return {"ok": False, "reason": f"库里没有 {start}~{end} 的日线数据"}
    px = build_returns(tmp, horizon=horizon, lag=1)
    res = evaluate_preset(px, growth_picker_factory(px), days=days, runs=runs, seed=seed)
    res["ok"] = True
    res["preset"] = "growth"
    res["caveat"] = RETROSPECT["growth"][1]
    res["universe"] = len(px["codes"])
    res["picks"] = 20          # 每日选股只数，前端用来写"随机抽 20 只"
    return res


# ---------------------------------------------------------------------------
# 自检（python newbie_baseline.py 直接跑）
# ---------------------------------------------------------------------------

def _selfcheck() -> int:
    """离线自检：不依赖数据库、不依赖网络。"""
    steps: List[Tuple[str, bool, str]] = []

    def rec(name: str, ok: bool, detail: str = "") -> None:
        steps.append((name, bool(ok), detail))

    # 1) 分位数与 numpy 对账
    s = sorted([1.0, 2.0, 3.0, 4.0, 5.0])
    rec("p50 中位数 = 3", quantile(s, 0.5) == 3.0, f"{quantile(s, 0.5)}")
    rec("p0 = 最小, p100 = 最大",
        quantile(s, 0.0) == 1.0 and quantile(s, 1.0) == 5.0)
    rec("p25 线性插值 = 2", abs(quantile(s, 0.25) - 2.0) < 1e-12, f"{quantile(s, 0.25)}")
    rec("空数组返回 None", quantile([], 0.5) is None)

    # 2) 百分位
    rec("百分位：最大值 = 100", percentile_rank(10.0, [1, 2, 3]) == 100.0)
    rec("百分位：最小值 = 0", percentile_rank(0.0, [1, 2, 3]) == 0.0)
    rec("百分位：严格小于（等于不算赢）",
        percentile_rank(2.0, [1, 2, 3]) == 100.0 / 3, f"{percentile_rank(2.0,[1,2,3])}")
    rec("空池返回 None", percentile_rank(1.0, []) is None)

    # 3) 前视防护：收益索引必须指向 t 之后
    c = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    r = forward_return(c, 0, horizon=5, lag=1)
    rec("forward_return 用 t+lag 买入（不用 t 当日价）",
        r is not None and abs(r - (c[6] / c[1] - 1)) < 1e-12,
        f"{r} 期望 {c[6]/c[1]-1}")
    rec("越界返回 None（不补零）", forward_return(c, 5, horizon=5) is None)
    rec("非正价格返回 None",
        forward_return(np.array([1.0, 0.0, 1.0]), 0, horizon=1) is None)

    # 4) 随机基线：策略与随机完全相同时，百分位应接近 50
    cands = [f"s{i}" for i in range(50)]
    rets = {f"s{i}": (i % 5) * 0.01 for i in range(50)}   # 离散收益，便于复现
    picks = cands[:10]
    b = random_baseline(cands, picks, rets, runs=300, seed=7)
    rec("同池抽样能跑满 runs", b["runs"] == 300, f"runs={b['runs']}")
    rec("百分位落在合理区间", b["percentile"] is not None and 0 <= b["percentile"] <= 100,
        f"{b['percentile']}")
    rec("p5 < p50 < p95",
        b["p5"] < b["p50"] < b["p95"], f"{b['p5']:.4f}/{b['p50']:.4f}/{b['p95']:.4f}")

    # 5) 同种子可复现 —— 这是整个基线可信的前提
    b2 = random_baseline(cands, picks, rets, runs=300, seed=7)
    rec("同种子结果完全一致", b["random"] == b2["random"])
    b3 = random_baseline(cands, picks, rets, runs=300, seed=8)
    rec("换种子结果不同", b["random"] != b3["random"])

    # 6) 策略明显更好时，百分位必须高
    best = sorted(cands, key=lambda c: -rets[c])[:10]
    bb = random_baseline(cands, best, rets, runs=300, seed=7)
    rec("策略选最赚的 10 只 → 百分位接近 100",
        bb["percentile"] is not None and bb["percentile"] >= 99,
        f"{bb['percentile']}")
    worst = sorted(cands, key=lambda c: rets[c])[:10]
    bw = random_baseline(cands, worst, rets, runs=300, seed=7)
    rec("策略选最亏的 10 只 → 百分位接近 0",
        bw["percentile"] is not None and bw["percentile"] <= 1,
        f"{bw['percentile']}")

    # 7) 不可比的情形必须如实报告，不能补零冒充
    small = random_baseline(["a", "b"], ["a", "b"], {"a": 0.01, "b": 0.02}, runs=10)
    rec("池子恰好等于选股数 → 可比（随机退化为唯一组合）",
        small["percentile"] is not None and small["note"] is None,
        f"pct={small['percentile']}")
    rec("池子等于选股数时百分位 = 50（与自身相同）",
        small["percentile"] is not None and abs(small["percentile"] - 50.0) < 1e-9,
        f"{small['percentile']}")
    short = random_baseline(["a", "b"], ["a", "b", "c"], {"a": 0.01, "b": 0.02}, runs=10)
    rec("池子小于选股数 → note 非空且 percentile 为 None",
        short["percentile"] is None and bool(short["note"]), short["note"] or "")
    miss = random_baseline(cands, picks, {}, runs=10)
    rec("策略无收益数据 → 不参与统计",
        miss["percentile"] is None and bool(miss["note"]), miss["note"] or "")

    # 8) 汇总：不可比天数必须显式暴露，且两个口径都要汇总
    days = [
        {"percentile": 90.0, "strategy": 0.02, "random": [0.01, 0.0],
         "matched": {"percentile": 60.0, "random": [0.015, 0.0]}},
        {"percentile": 80.0, "strategy": 0.01, "random": [0.005, 0.0],
         "matched": {"percentile": 55.0, "random": [0.009, 0.0]}},
        {"percentile": None, "strategy": None, "random": [], "matched": None, "note": "x"},
    ]
    ag = aggregate(days)
    rec("汇总：跳过天数 = 1", ag["days_skipped"] == 1, f"{ag['days_skipped']}")
    rec("汇总：可用天数 = 2", ag["days_usable"] == 2)
    rec("汇总：整池口径百分位均值 = 85",
        abs(ag["vs_all"]["percentile_mean"] - 85.0) < 1e-9,
        f"{ag['vs_all']['percentile_mean']}")
    rec("汇总：整池口径胜率 = 100%", abs(ag["vs_all"]["win_rate"] - 1.0) < 1e-9)
    rec("汇总：子池口径单独汇总（不被整池覆盖）",
        ag["vs_matched"]["days"] == 2
        and abs(ag["vs_matched"]["percentile_mean"] - 57.5) < 1e-9,
        f"days={ag['vs_matched']['days']} mean={ag['vs_matched']['percentile_mean']}")
    rec("汇总：给出结论", bool(ag["vs_all"]["verdict"]), ag["vs_all"]["verdict"])
    rec("全不可比时如实说样本不足",
        aggregate([{"percentile": None}])["vs_all"]["percentile_mean"] is None)

    # 9) 双口径必须同时可用，且能给出不同结论（本模块的核心设计）
    cands2 = [f"c{i}" for i in range(100)]
    # 构造：整池包含大量高收益股，子池只有低收益股 → 策略在整池里很差、在子池里还行
    rets2 = {c: (0.05 if int(c[1:]) >= 50 else -0.01) for c in cands2}
    picks2 = cands2[:20]                     # 全是 -0.01 那批
    sub_pool = cands2[:40]                   # 子池也只有 -0.01 那批
    d2 = random_baseline(cands2, picks2, rets2, runs=200, seed=1, matched_pool=sub_pool)
    rec("双口径同时返回", d2["percentile"] is not None and d2["matched"] is not None)
    rec("双口径结论确实不同（整池差、子池持平）",
        d2["percentile"] < 20 and abs(d2["matched"]["percentile"] - 50) < 1,
        f"整池 {d2['percentile']:.1f} / 子池 {d2['matched']['percentile']:.1f}")
    rec("不传 matched_pool 时 matched 为 None",
        random_baseline(cands2, picks2, rets2, runs=10)["matched"] is None)

    # 10) 历史回溯：合成一段可解析的行情，验证矩阵与端到端结论
    #     构造 30 只票、40 天。前 15 只每天涨 1%，后 15 只每天跌 1%。
    import datetime as _dt
    base_day = _dt.date(2026, 1, 5)
    synth: Dict[str, List[Tuple[str, float, float]]] = {}
    for j in range(30):
        code = f"t{j:02d}"
        px_ = 10.0
        seq = []
        for i in range(40):
            d = (base_day + _dt.timedelta(days=i)).isoformat()
            px_ *= (1.01 if j < 15 else 0.99)
            seq.append((d, px_, 1000.0))
        synth[code] = seq

    pxm = build_returns(synth, horizon=5, lag=1)
    rec("矩阵维度正确", pxm["ret"].shape == (40, 30), f"{pxm['ret'].shape}")
    # span = lag + horizon = 6 → 前 T-6=34 天有收益，末尾 6 天为 NaN
    rec("前 34 天有收益、后 6 天为 NaN（未到期）",
        np.isfinite(pxm["ret"][:34]).all() and not np.isfinite(pxm["ret"][34:]).any(),
        f"有效 {int(np.isfinite(pxm['ret'][:,0]).sum())}/40")
    # 这两条是给"最后一天到底能不能用"钉死的：曾经 `evaluate_preset` 自己
    # 重算 last，把最新 6 天整段跳过，自检全绿也发现不了。现在让 valid_end
    # 成为唯一口径，并断言 t=valid_end 的收益真的算得出来、且等于手工公式。
    rec("valid_end 指向最后一个有收益的行（T-span-1）",
        pxm["valid_end"] == 40 - 6 - 1, f"{pxm['valid_end']}")
    _ve = pxm["valid_end"]
    _manual = pxm["close"][_ve + 1 + 5, 0] / pxm["close"][_ve + 1, 0] - 1
    rec("valid_end 行的收益 = close[t+1+5]/close[t+1]-1（手工对账）",
        abs(float(pxm["ret"][_ve, 0]) - _manual) < 1e-12,
        f"{pxm['ret'][_ve, 0]:.6f} vs {_manual:.6f}")
    rec("无缺失价格（合成数据全覆盖）", np.isfinite(pxm["close"]).all())

    # 策略：永远买前 15 只（涨的）；候选池 = 全部 30 只；子池 = 前 15 只
    def _picker(t, ctx):
        return list(range(15)), list(range(30)), list(range(15))

    # 断言：取窗口的最后一天，必须是"最后一个有收益的日子"，不能更早。
    # 合成数据 40 行、span=6 → valid_end = 33，窗口末日就该是 pxm["dates"][33]，
    # 早一天就是在偷偷扔掉最新数据（真库上就发生过：98 行只用到第 91 天）。
    res = evaluate_preset(pxm, _picker, days=30, runs=100, seed=3)
    rec("端到端：可评估天数 > 0", res["days_usable"] > 0, f"{res['days_usable']}")
    rec("端到端：买上涨的一半 → 整池百分位接近 100",
        res["vs_all"]["percentile_mean"] is not None
        and res["vs_all"]["percentile_mean"] >= 99,
        f"{res['vs_all']['percentile_mean']}")
    rec("端到端：子池只有上涨的一半 → 百分位 = 50（无差别）",
        res["vs_matched"]["percentile_mean"] is not None
        and abs(res["vs_matched"]["percentile_mean"] - 50) < 1,
        f"{res['vs_matched']['percentile_mean']}")
    rec("端到端：结论文字正确", res["vs_all"]["verdict"] == "显著强于随机选股",
        res["vs_all"]["verdict"])
    rec("端到端：窗口信息完整",
        bool(res["window"]["from"]) and bool(res["window"]["to"])
        and res["window"]["horizon"] == 5)
    # 回归：窗口末日必须等于 valid_end 那天，否则就是又漏了最新几天
    rec("端到端：窗口末日 = 最后一个有收益的交易日",
        res["window"]["to"] == pxm["dates"][pxm["valid_end"]],
        f"{res['window']['to']} vs {pxm['dates'][pxm['valid_end']]}")

    # 回归（真库发现的）：旧实现 `last = T - span - 1` 让窗口末日停在 T-7，
    # 白丢最新 6 个交易日，而当时所有断言都是绿的 —— 合成数据和旧公式互相
    # "对上了"，谁也没错。现在把 truth 钉在 build_returns 的赋值区间上
    # （`ret[:T-span]` 有值 ⇒ 最后一个有收益的行下标是 T-span-1），
    # 任何人再动 evaluate_preset 的取窗口径都会撞死在这条断言上。
    # 特意用大 T（98）而不是 40，让差一天在比例上更像真库。
    _big = {f"z{i}": [(f"2026-{m:02d}-{d:02d}", 10.0 + 0.01 * i, 1e8)
                      for m in range(1, 6) for d in range(1, 21)] for i in range(5)}
    _pxb = build_returns(_big, horizon=5, lag=1)
    rec("valid_end 与 ret 赋值区间一致（ret[valid_end] 有值，valid_end+1 全 NaN）",
        np.isfinite(_pxb["ret"][_pxb["valid_end"]]).any()
        and not np.isfinite(_pxb["ret"][_pxb["valid_end"] + 1:]).any(),
        f"T={len(_pxb['dates'])} valid_end={_pxb['valid_end']}")

    # 11) 前视防护：把 picker 故意改成"看未来"，结论会异常高——证明检查能捕捉
    def _cheat(t, ctx):
        # 偷看 t 日收益，只买当天最赚的
        r = pxm["ret"][t]
        order = list(np.argsort(-np.where(np.isfinite(r), r, -1e9)))[:15]
        return order, list(range(30)), []

    cheat = evaluate_preset(pxm, _cheat, days=10, runs=50, seed=3)
    rec("作弊 picker（偷看未来）→ 百分位 100，说明基线对前视敏感",
        cheat["vs_all"]["percentile_mean"] is not None
        and cheat["vs_all"]["percentile_mean"] >= 99.9,
        f"{cheat['vs_all']['percentile_mean']}")

    passed = sum(1 for _, ok, _ in steps if ok)
    for name, ok, detail in steps:
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(steps)} PASS")
    return 0 if passed == len(steps) else 1


if __name__ == "__main__":
    import sys
    rc = _selfcheck()
    # `--real` 额外跑一次真实库评估：自检全绿只证明公式对，
    # 证明不了「接上真数据还能不能跑通」，两者要分开验。
    if "--real" in sys.argv:
        print("\n=== 真跑 evaluate_growth ===")
        import time as _t
        t0 = _t.time()
        r = evaluate_growth(runs=int(os.environ.get("RUNS", DEFAULT_RUNS)))
        print(f"耗时 {_t.time() - t0:.2f}s  ok={r.get('ok')}  reason={r.get('reason')}")
        if r.get("ok"):
            print("窗口", r.get("window"))
            print("宇宙", r.get("universe"))
            print(f"整池 {r['vs_all']['percentile_mean']:.1f} {r['vs_all']['verdict']}"
                  if r["vs_all"]["percentile_mean"] is not None else "整池 无数据")
            print(f"子池 {r['vs_matched']['percentile_mean']:.1f} {r['vs_matched']['verdict']}"
                  if r["vs_matched"]["percentile_mean"] is not None else "子池 无数据")
            print("每日明细条数", len(r.get("daily") or []))
            print("caveat:", r.get("caveat"))
    raise SystemExit(rc)
