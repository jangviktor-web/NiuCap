"""策略有效性评估（IC / ICIR）。

回答一个此前无人回答的问题：**我们这 20 多个选股策略，哪个真的有效？**

背景
----
`screener.py` 里有 22 个策略（16 个日线 + 6 个分钟级），但都是「人工定义
+ 直觉判断」，从来没有用历史数据度量过预测力。本模块补上这一环：把每个
策略在历史上每个交易日产出的「命中集合」当作 0/1 因子，算它与未来收益的
截面 Spearman 相关（IC），再按天求均值与稳定性（ICIR）。

判读口径
--------
    IC 均值     方向与强度。>0 说明「命中后倾向上涨」，<0 是反向指标。
    ICIR        IC 均值 / IC 标准差。衡量**稳定性**，比单看 IC 更重要：
                偶尔 IC 很高但方向乱跳的策略，ICIR 会很低，不可用。
    IC>0 比例   IC 为正的天数占比。接近 50% 说明方向不稳定。

经验参考（业界惯例，非本项目硬门槛）：
    |ICIR| > 0.5  较强
    |ICIR| > 0.3  中等
    |ICIR| < 0.1  基本无预测力

为什么只评 9 个策略
-------------------
策略函数依赖「当日快照」的字段，历史回测要从日线重建快照。`daily_bars`
只有 OHLCV + amount，**没有 turnover（换手率）/ total_cap（总市值）/
name（名称）**——这三个无法从日线推算（需要流通股本 / 总股本，库里没有）。

AST 精确扫描的结果：
    完全可回测  9 个  → EVALUABLE_KEYS
    部分缺失    5 个  → 缺 turnover / total_cap / name 之一
    重度缺失    2 个  → 缺多个字段

对缺失字段的策略，本模块**返回 skipped 而不是猜一个值**。用代用指标算出的
IC 会误导使用者（比如用「成交量排名」冒充「市值」）。宁可标注「暂不评估」。

时序正确性（第一版踩的坑）
--------------------------
最初直接用完整引擎逐日跑策略，结果全错：`history.HistoryEngine.metrics()`
用 `C[-1]` 取**最后一根**，传完整引擎时每天算的都是「最新交易日」的指标，
时序完全错位。症状是 `breakout_20h` 日均命中 2005 只（占全市场 36%，正常
约 300~400 只），IC 算出 -0.19 的假信号。

修法是构造 `_TruncatedEngine`：把每只股票的 ndarray 切到目标日为止，
`metrics()` 的尾部索引因此自动落在目标日。切片正确性已用「手工算 ma20
与引擎输出比对」验证过。
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import re
import threading
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# 策略分级用的经验阈值
# ---------------------------------------------------------------------------
#: |ICIR| 达到此值算「较强」
LEVEL_STRONG = 0.5
#: |ICIR| 达到此值算「中等」
LEVEL_MID = 0.3
#: |ICIR| 低于此值视为基本无预测力
LEVEL_WEAK = 0.1


# ---------------------------------------------------------------------------
# 可评估策略白名单
#
# 与 screener.STRATEGY_DEFS 的 key 对应。只收录「字段完全可重建」的策略。
# ---------------------------------------------------------------------------
EVALUABLE_KEYS: Tuple[str, ...] = (
    "breakout_20h",
    "ma_bull",
    "platform_break",
    "gap_up",
    "volume_surge",
    "oversold_rebound",
    "reversal_hammer",
    "pullback_ma20",
    "pattern_score",
    # —— 方案 A · 经典指标（蒸馏自 cinar/indicator）——
    # 全部只用 OHLCV+amount（daily_bars 可直接重建），无缺失字段，可评估。
    "supertrend_long",
    "atr_breakout",
    "connors_rsi_dip",
    "td9_buy",
    "mfi_oversold",
    "cmf_breakout",
    # boll_squeeze 暂不入体检白名单：它的作用是「筛出候选票」，历史超额收益
    # 还没测过。体检表每加一行都是全市场 × N 天的开销，没测过的别占位。
)

#: 不可评估策略的缺字段情况（key -> 缺失字段名列表）
#: 用于给前端展示「为什么这个策略没法评估」，避免用户以为「忘了做」。
UNEVALUABLE_FIELDS: Dict[str, List[str]] = {
    "limit_up": ["name"],
    "near_limit": ["name"],
    "strong_turnover": ["turnover"],
    "vol_price_up": ["turnover"],
    "low_vol_leader": ["total_cap"],
    "limit_momentum": ["name", "turnover"],
    "small_cap_active": ["total_cap", "turnover"],
}

#: 字段 -> 人话解释
FIELD_REASON = {
    "turnover": "需流通股本才能算换手率，日线库没有该数据",
    "total_cap": "需总股本才能算市值，日线库没有该数据",
    "name": "日线库不含股票名称，无法做 ST 判定",
}


def _reason_of(fields: Sequence[str]) -> str:
    return "；".join(FIELD_REASON.get(f, f"缺少字段 {f}") for f in fields)


class _TruncatedEngine:
    """历史引擎的「截至某日」只读视图。

    `history.HistoryEngine.metrics()` 通过 `C[-1]`、`H[:-1]` 这类尾部索引
    取值。把 ndarray 切到目标日后，同一份 metrics 实现会自动返回「目标日」
    的指标——因此**无需修改 history.py**，对既有代码零侵入。
    """

    def __init__(self, base: Any, as_of_date: str):
        self.as_of = as_of_date
        self._data: Dict[str, Dict[str, Any]] = {}

        for code, s in base._data.items():
            dates = s["dates"]
            # 二分找最后一个 <= as_of_date 的位置
            lo, hi = 0, len(dates)
            while lo < hi:
                mid = (lo + hi) // 2
                if dates[mid] <= as_of_date:
                    lo = mid + 1
                else:
                    hi = mid
            if lo < 1:
                continue
            self._data[code] = {
                "close": s["close"][:lo],
                "high": s["high"][:lo],
                "low": s["low"][:lo],
                "open": s["open"][:lo],
                "volume": s["volume"][:lo],
                "amount": s["amount"][:lo],
                "dates": dates[:lo],
            }

    def metrics(self, code: str, need: Optional[Sequence[str]] = None):
        """复用 HistoryEngine 的指标实现，但作用在截断后的数据上。"""
        import history
        return history.HistoryEngine.metrics(self, code, need)

    def series(self, code: str):
        """取截断后的原始序列（不含指标）。

        让 screener 的新策略（`s_supertrend_long` 等）能以与真实引擎一致
        的接口 `hist.series(code)` 取到「截至目标日」的 OHLCV，从而自算
        ATR / SuperTrend / ConnorsRSI 等经典指标——时序正确性由这里的
        截断天然保证，无需策略层自己关心「历史停在哪天」。
        """
        return self._data.get(code)


def _build_axis(eng: Any, min_coverage: int = 3000) -> List[str]:
    """构造统一交易日轴。

    不直接取「所有出现过的日期」——停牌股、新股会让日期集合很碎。只保留
    **当天有 >= min_coverage 只股票有数据**的日期，保证截面样本量足够，
    IC 才稳定。
    """
    cnt: Counter = Counter()
    for s in eng._data.values():
        for d in s["dates"]:
            cnt[d] += 1
    return sorted(d for d, n in cnt.items() if n >= min_coverage)


def _spearman(x: np.ndarray, y: np.ndarray) -> Optional[float]:
    """Spearman 秩相关（等价于对秩做 Pearson）。

    对 0/1 信号而言比 Pearson 更合适：只看排序，不会因为「命中股票占比
    很小」而把相关系数机械地压低。
    """
    n = len(x)
    if n < 3:
        return None

    # 输入本身必须为非常量。注意：不能靠「秩的方差」来判断——
    # np.ones(50) 做 argsort 后秩仍是 0..49（方差不为 0），会被漏判。
    if float(np.std(x)) < 1e-12 or float(np.std(y)) < 1e-12:
        return None

    def _rank(a: np.ndarray) -> np.ndarray:
        order = a.argsort()
        r = np.empty(n, dtype=np.float64)
        r[order] = np.arange(n, dtype=np.float64)
        return r

    rx = _rank(x)
    ry = _rank(y)
    rx -= rx.mean()
    ry -= ry.mean()
    den = math.sqrt(float((rx ** 2).sum()) * float((ry ** 2).sum()))
    if den < 1e-12:
        return None      # 信号全相同，无相关可言
    return float((rx * ry).sum() / den)


def _level(icir: float) -> str:
    """把 ICIR 映射成人话等级。"""
    a = abs(icir)
    if a >= LEVEL_STRONG:
        return "strong"
    if a >= LEVEL_MID:
        return "mid"
    if a >= LEVEL_WEAK:
        return "weak"
    return "none"


# ---------------------------------------------------------------------------
# 缓存：评估很慢（9 策略 × 120 天约 90 秒），必须缓存
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Any] = {"key": None, "ts": 0.0, "value": None}
_CACHE_TTL = 6 * 3600.0


# 命中序列缓存（模拟净值的数据底座，见 equity_sim.py）
#
# evaluate() 真算时，逐日循环里已经得到了「每个策略每天命中哪些股票」
# （hits）。净值模拟需要的正是这份数据——但结果 JSON 里不带它（太大）。
# 所以在内存里单独留一份：评估完成时整体替换引用，模拟接口直接取用。
# 与 _CACHE 同生共死：evaluate() 缓存命中（提前 return）时两者都不变，
# 因此「_CACHE 有值 ⟺ _HITS_CACHE 有值」永远成立，不会错位。
_HITS_CACHE: Dict[str, Any] = {}          # {eval_days, hits, as_of}


def get_hits() -> Dict[str, Any]:
    """取最近一次 evaluate() 的命中序列。没有则返回空 dict。"""
    return _HITS_CACHE


def hits_ready(days: int, forward: int) -> bool:
    """体检缓存是否已就绪（同参数、未过期、数据没变）。

    模拟净值依赖体检的副产物（命中序列）。体检没跑过就请求模拟时，
    用这个快速判断——而不是让模拟接口悄悄触发一次 40 秒的全量重算。
    注意不能走 _cache_key()：那里面会 get_engine() 触发引擎懒加载
    （9 秒），这里只想「看一眼」缓存，绝不引发加载。
    """
    import history
    eng = history.get_engine(auto_load=False)
    if not eng or not eng._data:
        return False
    as_of = getattr(eng, "_as_of", "") or ""
    k = f"{days}|{forward}|{as_of}|def"
    return (_CACHE["key"] == k
            and (time.time() - _CACHE["ts"]) < _CACHE_TTL)


def _cache_key(days: int, forward: int,
               lookback_days: Optional[int] = None) -> str:
    """缓存键。

    含 lookback 维度：全量引擎（lookback_days 大）与默认 760 天引擎的
    as_of（最新交易日）相同，若不区分会互相命中错误缓存。
    """
    import history
    eng = history.get_engine()
    as_of = getattr(eng, "_as_of", "") or ""
    tag = lookback_days if lookback_days is not None else "def"
    return f"{days}|{forward}|{as_of}|{tag}"


def evaluate(
    days: int = 120,
    forward: int = 5,
    keys: Optional[Sequence[str]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    use_cache: bool = True,
    lookback_days: Optional[int] = None,
    engine: Optional[Any] = None,
    min_coverage: Optional[int] = None,
) -> Dict[str, Any]:
    """跑策略有效性评估。

    参数
    ----
    days / forward / keys / progress / use_cache  见原函数说明。
    lookback_days  历史回溯天数。默认 None → 用全局引擎（约 760 天，面板
                  实测口径）。传大值（如 10000）则临时加载全量历史，让选股
                  因子也能跨牛熊体检。仅当 engine 为 None 时生效。
    engine        直接传入已加载的 HistoryEngine（绕过全局缓存）。与
                  lookback_days 二选一；同时传则 engine 优先。
    min_coverage  截面覆盖门槛（单日有数据的股票数下限）。默认 None → 3000
                  （面板口径）。早期年份只回填了部分指数股时，可降到 ~1000
                  以纳入那段历史，否则会被 _build_axis 丢弃。
    """
    t0 = time.time()

    cache_k = _cache_key(days, forward, lookback_days)
    if use_cache and _CACHE["key"] == cache_k and \
            (time.time() - _CACHE["ts"]) < _CACHE_TTL:
        out = dict(_CACHE["value"])
        out["cached"] = True
        print(f"[strategy-eval] 缓存命中 {cache_k}")
        return out

    import history
    import screener

    if engine is not None:
        eng = engine
    elif lookback_days is not None:
        eng = history.HistoryEngine(lookback_days=lookback_days).load(force=True)
    else:
        eng = history.get_engine()
    if not eng or not eng._data:
        return {"ok": False, "error": "历史数据未加载，请先等待数据就绪"}

    want_keys = [k for k in (keys or EVALUABLE_KEYS) if k in EVALUABLE_KEYS]

    axis = _build_axis(eng, min_coverage=min_coverage if min_coverage is not None
                       else 3000)
    if len(axis) < forward + 20:
        return {"ok": False, "error": f"历史交易日不足（仅 {len(axis)} 天）"}

    # 市况标签（方案 D）：全市场等权指数的 Chop 逐日 tag（trend/range/
    # transition）。一次构建按数据版本缓存，聚合阶段按 eval 日查表分桶。
    _regime_tag: Dict[str, Optional[str]] = {}
    try:
        import market_breadth as _mb
        _rg = _mb.market_regime_series(eng)
        _regime_tag = {dd: tt for dd, tt in zip(_rg["dates"], _rg["tag"])}
    except Exception:
        _regime_tag = {}

    # 评估窗口：尾部留出 forward 天，用于计算未来收益
    eval_days = axis[-(days + forward):-forward]
    if not eval_days:
        return {"ok": False, "error": "评估窗口为空，请增大 days 或减少历史缺口"}

    # (code, date) -> 索引。注意：索引基于**完整**引擎，未来收益要读全序列。
    idx_map = {c: {d: i for i, d in enumerate(s["dates"])}
               for c, s in eng._data.items()}

    hits: Dict[str, Dict[str, set]] = {k: {} for k in want_keys}
    fwd_map: Dict[str, Dict[str, float]] = {}
    universe_sizes: List[int] = []

    # pattern_score 是连续分数（0~100），值得做 5 档分层；其它策略是 0/1
    # 命中，只能分「命中 vs 其余」两组。分数在这里逐日收集，聚合阶段统一算。
    score_map: Dict[str, Dict[str, float]] = {}
    want_quantiles = "pattern_score" in want_keys

    total = len(eval_days)
    for di, dt in enumerate(eval_days):
        if progress and (di % 5 == 0 or di == total - 1):
            try:
                progress(di, total, dt)
            except Exception:
                pass

        te = _TruncatedEngine(eng, dt)

        # 重建当日快照
        rows: List[Dict[str, Any]] = []
        fwd: Dict[str, float] = {}
        for c, s in te._data.items():
            if len(s["close"]) < 2:
                continue
            px = float(s["close"][-1])
            prev = float(s["close"][-2])
            if prev <= 0 or px <= 0:
                continue
            rows.append({
                "code": c, "price": px, "prev_close": prev,
                "open": float(s["open"][-1]), "high": float(s["high"][-1]),
                "low": float(s["low"][-1]), "volume": float(s["volume"][-1]),
                "amount": float(s["amount"][-1]),
                "change_pct": (px / prev - 1) * 100,
                # 以下三个字段日线无法重建，留空。用到它们的策略不在 EVALUABLE_KEYS 里。
                "name": "", "total_cap": 0.0, "turnover": 0.0,
            })
            i = idx_map.get(c, {}).get(dt)
            if i is not None:
                j = i + forward
                full = eng._data[c]["close"]
                if j < len(full):
                    fwd[c] = (float(full[j]) / px - 1) * 100

        fwd_map[dt] = fwd
        universe_sizes.append(len(rows))
        if len(rows) < 100:
            continue

        # pattern_score 的全市场分数（与 s_pattern_score 同一套打分，只是
        # 不过 85 分门槛）——分层测试要看「分数越高是否越赚」，必须有原始分
        if want_quantiles:
            try:
                import similarity as _sim
                _sc: Dict[str, float] = {}
                for r in rows:
                    s = te._data.get(r["code"])
                    if not s:
                        continue
                    try:
                        res = _sim.score_series_at(s, len(s["close"]) - 1)
                    except Exception:
                        continue
                    if res:
                        _sc[r["code"]] = float(res[0])
                score_map[dt] = _sc
            except Exception:
                pass

        # 跑每个策略
        for key in want_keys:
            d = screener.STRATEGY_BY_KEY.get(key)
            if not d:
                continue
            kw: Dict[str, Any] = {"hist": te} if d.get("hist") else {}
            try:
                h = d["fn"](rows, **kw)
                hits[key][dt] = h if isinstance(h, set) else set(h or [])
            except Exception:
                # 单日失败不该中断整轮评估，记为当天无命中
                hits[key][dt] = set()

    # ---- 聚合 IC ----
    items: List[Dict[str, Any]] = []
    for key in want_keys:
        d = screener.STRATEGY_BY_KEY.get(key) or {}
        ic_list: List[float] = []
        n_hits: List[int] = []

        # ---- 分层测试（比 IC 更直观的证据）----
        # 所有策略：命中组 vs 其余组的未来收益对比（0/1 因子只有这两组）；
        # pattern_score 是连续分数，另做 5 档等频分层，看「分数越高越赚」
        # 是否单调。全部用日频收益的简单平均，口径与 IC 一致。
        hit_rets: List[float] = []
        rest_rets: List[float] = []
        # 市况分桶（方案 D）：把 hit/rest 收益按当日 Chop 标签分到趋势/震荡市
        t_hit: List[float] = []
        t_rest: List[float] = []
        r_hit: List[float] = []
        r_rest: List[float] = []
        q_rets: List[List[float]] = [[] for _ in range(5)]
        for dt in eval_days:
            hit = hits.get(key, {}).get(dt, set())
            fwd = fwd_map.get(dt, {})
            if not fwd:
                continue
            _tag = _regime_tag.get(dt)
            for c, ret in fwd.items():
                if c in hit:
                    hit_rets.append(ret)
                    if _tag == "trend":
                        t_hit.append(ret)
                    elif _tag == "range":
                        r_hit.append(ret)
                else:
                    rest_rets.append(ret)
                    if _tag == "trend":
                        t_rest.append(ret)
                    elif _tag == "range":
                        r_rest.append(ret)
            sc = score_map.get(dt) if key == "pattern_score" else None
            if sc:
                pair = sorted(((sc[c], r) for c, r in fwd.items() if c in sc),
                              key=lambda x: x[0])
                if len(pair) >= 25:          # 样本太少分 5 组没意义
                    vals = np.array([p[1] for p in pair])
                    for gi, chunk in enumerate(np.array_split(vals, 5)):
                        if len(chunk):
                            q_rets[gi].append(float(chunk.mean()))

        hit_avg = float(np.mean(hit_rets)) if hit_rets else None
        rest_avg = float(np.mean(rest_rets)) if rest_rets else None
        hit_win = (float(np.mean([1.0 if x > 0 else 0.0 for x in hit_rets]))
                   if hit_rets else None)
        excess = (hit_avg - rest_avg) if (hit_avg is not None
                                          and rest_avg is not None) else None
        layers = None
        if hit_avg is not None:
            layers = {
                "hit_avg_ret": round(hit_avg, 3),
                "rest_avg_ret": round(rest_avg, 3),
                "hit_win_rate": round(hit_win, 3),
                "excess": round(excess, 3),
                "obs": len(hit_rets),
            }

        # 市况敏感度（方案 D）：趋势市 / 震荡市的命中超额对比。两组都需足够
        # 样本（≥20 个观察点）才下结论，否则标 None（避免噪声误判）。
        def _regime_excess(h: List[float], rr: List[float]) -> Optional[float]:
            if len(h) < 10 or len(rr) < 10:
                return None
            return float(np.mean(h)) - float(np.mean(rr))

        trend_ex = _regime_excess(t_hit, t_rest)
        range_ex = _regime_excess(r_hit, r_rest)
        fav = None
        if trend_ex is not None and range_ex is not None:
            fav = "trend" if trend_ex >= range_ex else "range"
        regime = {
            "trend_excess": (round(trend_ex, 3)
                             if trend_ex is not None else None),
            "range_excess": (round(range_ex, 3)
                             if range_ex is not None else None),
            "fav": fav,
            "trend_obs": len(t_hit) + len(t_rest),
            "range_obs": len(r_hit) + len(r_rest),
        }
        quantiles = None
        if key == "pattern_score" and all(g for g in q_rets):
            qmeans = [float(np.mean(g)) for g in q_rets]
            mono = _spearman(np.arange(5, dtype=float), np.array(qmeans))
            quantiles = {"means": [round(v, 3) for v in qmeans],
                         "mono_corr": (round(mono, 3) if mono is not None
                                       else None)}

        for dt in eval_days:
            hit = hits.get(key, {}).get(dt, set())
            fwd = fwd_map.get(dt, {})
            if not fwd:
                continue
            n_hits.append(len(hit))

            codes = list(fwd.keys())
            if len(codes) < 30:
                continue
            sig = np.array([1.0 if c in hit else 0.0 for c in codes])
            ret = np.array([fwd[c] for c in codes])
            # 命中太少或全命中 → 截面无区分度，跳过
            if sig.sum() < 3 or sig.sum() == len(sig):
                continue
            ic = _spearman(sig, ret)
            if ic is not None:
                ic_list.append(ic)

        if not ic_list:
            items.append({
                "key": key, "name": d.get("name", key), "cat": d.get("cat", ""),
                "ic_mean": None, "icir": None, "ic_pos_ratio": None,
                "avg_hits": None, "sample_days": 0,
                "level": "insufficient",
                "direction": "unknown",
                "note": "有效样本不足（命中数过少或过于集中）",
                "layers": layers, "quantiles": quantiles,
                "regime": regime,
            })
            continue

        arr = np.array(ic_list)
        m = float(arr.mean())
        sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        icir = m / sd if sd > 1e-9 else 0.0
        pos = float((arr > 0).mean())

        items.append({
            "key": key,
            "name": d.get("name", key),
            "cat": d.get("cat", ""),
            "ic_mean": round(m, 4),
            "icir": round(icir, 3),
            "ic_pos_ratio": round(pos, 3),
            "avg_hits": int(np.mean(n_hits)) if n_hits else 0,
            "sample_days": len(ic_list),
            "level": _level(icir),
            "direction": "positive" if m > 0 else "negative",
            "layers": layers, "quantiles": quantiles,
            "regime": regime,
        })

    # 按 |ICIR| 降序，让最有效的排最前
    items.sort(key=lambda x: abs(x.get("icir") or 0), reverse=True)

    skipped = []
    for k in screener.STRATEGY_DEFS:
        key = k["key"]
        if k.get("intraday"):
            continue
        if key in EVALUABLE_KEYS:
            continue
        fields = UNEVALUABLE_FIELDS.get(key)
        skipped.append({
            "key": key,
            "name": k.get("name", key),
            "cat": k.get("cat", ""),
            "reason": _reason_of(fields) if fields else "依赖非日线字段，暂不评估",
        })

    universe = int(np.median(universe_sizes)) if universe_sizes else 0

    out = {
        "ok": True,
        "as_of": eng._as_of,
        "span": [eval_days[0], eval_days[-1]],
        "days": len(eval_days),
        "forward": forward,
        "cost_seconds": round(time.time() - t0, 1),
        "items": items,
        "skipped": skipped,
        "universe": universe,
        "cached": False,
    }

    _prev_key = _CACHE["key"]
    _CACHE["key"] = cache_k
    _CACHE["ts"] = time.time()
    _CACHE["value"] = dict(out)
    print(f"[strategy-eval] 评估完成 {cache_k} 耗时 {out['cost_seconds']}s"
          + (f" · 覆盖了 {_prev_key}" if _prev_key and _prev_key != cache_k
             else ""))

    # 命中序列留给模拟净值用（整体替换引用，读端无需加锁）。
    # 仅在「默认 760 天」评估时写——全量/自定义引擎的评估是一次性的跨周期
    # 分析，绝不能覆盖 equity_sim 依赖的默认命中缓存，否则净值会用错截面。
    _is_default_eval = (lookback_days is None and engine is None)
    if _is_default_eval:
        _HITS_CACHE.clear()
        _HITS_CACHE["eval_days"] = list(eval_days)
        _HITS_CACHE["hits"] = {k: dict(d) for k, d in hits.items()}
        _HITS_CACHE["as_of"] = eng._as_of
        _HITS_CACHE["forward"] = forward
        _HITS_CACHE["cost_seconds"] = out["cost_seconds"]
        _HITS_CACHE["n_strategies"] = len(items)
        _HITS_CACHE["computed_at"] = time.time()
    return out


def cache_status() -> Dict[str, Any]:
    """体检缓存当前占用情况，供后台管理界面展示。

    缓存是单槽（按 days|forward|as_of 整份替换），这里把槽里现在装的是哪组
    参数、什么时候算的、覆盖多少策略都暴露出来，免得管理员对着「模拟净值」
    卡片干等还不知道是缓存没预热。
    """
    c = dict(_HITS_CACHE)
    ready = bool(c.get("eval_days")) and bool(c.get("hits"))
    days = len(c.get("eval_days") or [])
    return {
        "ready": ready,
        "days": days,
        "forward": c.get("forward"),
        "as_of": c.get("as_of"),
        "n_strategies": c.get("n_strategies", 0),
        "cost_seconds": c.get("cost_seconds"),
        "computed_at": c.get("computed_at"),
        "prewarm": dict(_PREWARM_LAST),
        # #98 每日自动预热调度：面板要显示「下次几点、为什么这次不用算」
        "schedule": prewarm_schedule_state(),
    }


def clear_cache() -> Dict[str, Any]:
    """清空体检缓存。下次「模拟净值」会提示先体检，而不是用陈旧结果。

    注意：只清命中序列，不碰正在跑的预热线程（prewarm 跑完会重新填槽）。
    """
    had = bool(_HITS_CACHE.get("hits"))
    _HITS_CACHE.clear()
    return {"ok": True, "cleared": had}


def evaluate_single(key: str, days: int = 120, forward: int = 5) -> Dict[str, Any]:
    """评估单个策略。走同一个缓存，所以连续查多个策略不会重复计算。"""
    if key not in EVALUABLE_KEYS:
        fields = UNEVALUABLE_FIELDS.get(key)
        return {
            "ok": False, "key": key,
            "error": _reason_of(fields) if fields else "该策略不可评估",
            "evaluable": False,
        }
    full = evaluate(days=days, forward=forward)
    if not full.get("ok"):
        return full
    for it in full["items"]:
        if it["key"] == key:
            return {"ok": True, "evaluable": True, **it}
    return {"ok": False, "key": key, "error": "未找到该策略的评估结果"}


# ---------------------------------------------------------------------------
# 落库后自动预热
#
# 一次评估要跑 90~150 秒，让用户点按钮时干等着很难受。每天收盘数据落库
# 完成后，数据反正变了，顺手在后台把默认参数（120 天 / 未来 5 日）重算
# 一遍写进缓存——第二天用户打开页面直接就是现成结果。
# ---------------------------------------------------------------------------

_PREWARM_LOCK = threading.Lock()    # 同一时刻只允许一个预热线程
_PREWARM_LAST: Dict[str, Any] = {"date": "", "key": ""}   # 今天预热过的参数


def prewarm(days: int = 120, forward: int = 5,
            only_if_synced_today: bool = False, why: str = "") -> Dict[str, Any]:
    """起后台线程预热体检缓存，立即返回。

    参数
    ----
    days / forward          预热哪组参数（默认与前端默认选项一致）。
    only_if_synced_today    服务重启补跑用：只在「今天已落库成功」时才预热，
                            否则没有新数据，重算纯属浪费 CPU。
    why                     日志里说明触发来源（落库完成 / 启动补跑）。
    """
    import datetime as _dt

    today = _dt.date.today().isoformat()

    if only_if_synced_today:
        try:
            import store as _st
            if _st.meta_get("sync_bars_last_ok", "") != today:
                print(f"[strategy-eval] 启动预热跳过：今天({today})尚未落库成功")
                return {"ok": False, "note": "今天尚未落库，跳过预热"}
        except Exception:
            print("[strategy-eval] 启动预热跳过：读取落库状态失败")
            return {"ok": False, "note": "读取落库状态失败，跳过预热"}

    if _PREWARM_LAST["date"] == today and _PREWARM_LAST["key"] == _cache_key(days, forward, None):
        return {"ok": False, "note": "今天这组参数已预热过"}

    if not _PREWARM_LOCK.acquire(blocking=False):
        return {"ok": False, "note": "已有预热在跑"}

    def _run():
        t0 = time.time()
        try:
            # 落库只写了 SQLite，内存历史引擎还停在旧数据——不强制重载的话
            # as_of 还是昨天的，等于用旧数据白算一遍
            import history
            history.reload_engine()
            r = evaluate(days=days, forward=forward, use_cache=False)
            _PREWARM_LAST["date"] = today
            _PREWARM_LAST["key"] = _cache_key(days, forward, None)
            print(f"[strategy-eval] 预热完成（{why or '落库后'}）: "
                  f"{r['days']} 个交易日 · as_of={r['as_of']} · "
                  f"耗时 {r['cost_seconds']}s")
            # 落库状态给每日调度看：今天算完了，到点就不用再检查
            try:
                import store as _st
                _st.meta_set(K_PW_OK, today)
                _st.meta_set(K_PW_RESULT,
                             f"{_now_text()} 完成：{r['days']} 个交易日 · "
                             f"as_of={r['as_of']} · {r['cost_seconds']}s")
            except Exception as _e:
                print(f"[strategy-eval] 预热状态落库失败（不影响结果）：{_e}")
            # 顺手把市场宽度也算热：引擎刚重载过，全市场聚合仅 ~1.2s。
            # 不做这步的话，重启后头 2 分钟打开首页，宽度卡片会与体检
            # 预热抢 CPU，首算可能被拖到 1 分半（实测 98s），逼近网关超时
            try:
                import market_breadth
                _tb = time.time()
                market_breadth.compute(days=250, use_cache=False)
                print(f"[strategy-eval] 预热附带：市场宽度已就绪 "
                      f"{time.time() - _tb:.1f}s")
            except Exception as e:
                print(f"[strategy-eval] 预热附带宽度失败: "
                      f"{type(e).__name__}: {e}")
        except Exception as e:
            print(f"[strategy-eval] 预热失败（{why or '落库后'}）: "
                  f"{type(e).__name__}: {e}")
        finally:
            _PREWARM_LOCK.release()

    threading.Thread(target=_run, daemon=True, name="eval-prewarm").start()
    return {"ok": True, "note": f"预热已启动（days={days}, {why or '落库后'}）"}


# ---------------------------------------------------------------------------
# 每日自动预热调度
#
# 为什么要单独一个调度，而不是继续搭日线落库的车：
#   落库只在交易日跑。周末 / 节假日 / 当天落库失败，就没人预热，缓存一直
#   停在昨天，体检页打开发现是陈的。搭车的三个触发点（落库后、启动补跑、
#   手动按钮）全都依赖「今天落库成功」这一个前提。
#
# 为什么按数据判脏，而不是每天无脑算一遍：
#   实测单次评估 230 秒（不是注释里写的 90~150 秒），CPU 满载跑 4 分钟。
#   而判脏只读 daily_bars 的 MAX(date)——走 idx_bars_date，实测 0.000 秒。
#   差六个数量级，没有任何理由不算清楚就重算。
#
# 三个做法照抄 scheduler.py（那套已经跑了很久，不重新发明）：
#   1. 状态存 meta 表 —— 服务重启后依然知道「今天处理过没」，不需补跑逻辑。
#   2. 用日期字符串比对，不用时间差 —— 系统休眠 / 时钟漂移都不会漏或重。
#   3. 时间配置不合法就回落默认 —— 脏值永不生效。
# ---------------------------------------------------------------------------

K_PW_TRY = "eval_prewarm_last_try"      # 最近一次「到点检查」的日期
K_PW_OK = "eval_prewarm_last_ok"        # 最近一次「真的算完」的日期
K_PW_RESULT = "eval_prewarm_last_result"  # 一句话结果，给人看
DEFAULT_PREWARM_AT = "16:30"            # 落库 15:30 之后 1 小时，留足缓冲
_AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_pw_thread: Optional[threading.Thread] = None
_pw_stop = threading.Event()


def _now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def prewarm_at() -> str:
    """每日预热时间点。环境变量 TICK_EVAL_PREWARM_AT 覆盖，脏值回落 16:30。"""
    v = (os.environ.get("TICK_EVAL_PREWARM_AT") or "").strip()
    return v if _AT_RE.match(v) else DEFAULT_PREWARM_AT


def db_latest_date() -> str:
    """daily_bars 最新交易日。走 idx_bars_date，实测 0.000s，可放心每分钟读。"""
    try:
        import store as _st
        r = _st._conn().execute("SELECT MAX(date) FROM daily_bars").fetchone()
        return str(r[0] or "") if r else ""
    except Exception:
        return ""


def prewarm_due(days: int = 120, forward: int = 5) -> Dict[str, Any]:
    """该不该重算体检缓存。返回 {due, reason, db_max_date, cache_as_of}。

    判据按优先级：
      ① 缓存为空      → 必算。手动清空后靠这条自愈，否则永远不预热。
      ② 缺 as_of 标记 → 算。缓存结构不完整，判不了脏就按脏处理。
      ③ 数据日期变了  → 算。以 DB 的 MAX(date) 为权威基准（引擎 as_of 可能
                        因未 reload 而滞后，不能拿它当基准）。
      ④ 其余          → 不算，并给出人话原因（周末 / 落库失败当天落这里）。
    """
    as_of = str(_HITS_CACHE.get("as_of") or "")
    dbmax = db_latest_date()
    if not _HITS_CACHE.get("hits") or not _HITS_CACHE.get("eval_days"):
        return {"due": True, "reason": "缓存为空，需要首次计算",
                "db_max_date": dbmax, "cache_as_of": as_of}
    if not as_of:
        return {"due": True,
                "reason": f"缓存缺少数据日期标记，需重算（数据至 {dbmax or '—'}）",
                "db_max_date": dbmax, "cache_as_of": ""}
    if dbmax and dbmax != as_of:
        return {"due": True, "reason": f"数据已更新到 {dbmax}，缓存停在 {as_of}",
                "db_max_date": dbmax, "cache_as_of": as_of}
    return {"due": False,
            "reason": f"数据未更新（最新 {dbmax or as_of}），无需重算",
            "db_max_date": dbmax, "cache_as_of": as_of}


def _prewarm_next_text() -> str:
    """下一次触发时间。**只做估算**：跳过周末，不管节假日。

    真实算不算由 prewarm_due() 的数据判脏决定——这里只是给 panel 显示一句
    人话，不做网络请求（scheduler.py 里也是这个取舍）。
    """
    hh, mm = (int(x) for x in prewarm_at().split(":"))
    now = _dt.datetime.now()
    tgt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tgt <= now:
        tgt += _dt.timedelta(days=1)
    while tgt.weekday() >= 5:            # 周六周日顺延
        tgt += _dt.timedelta(days=1)
    return tgt.strftime("%Y-%m-%d %H:%M")


def prewarm_schedule_state(days: int = 120, forward: int = 5) -> Dict[str, Any]:
    """给后台面板看的调度状态。**只读**，不触发任何计算。"""
    try:
        import store as _st
        try_ = _st.meta_get(K_PW_TRY, "") or ""
        ok = _st.meta_get(K_PW_OK, "") or ""
        result = _st.meta_get(K_PW_RESULT, "") or ""
    except Exception as e:
        try_, ok, result = "", "", f"读取调度状态失败：{type(e).__name__}: {e}"
    today = _dt.date.today().isoformat()
    due = prewarm_due(days, forward)
    return {
        "enabled": _pw_thread is not None and _pw_thread.is_alive(),
        "at": prewarm_at(),
        "today": today,
        "last_try": try_,
        "last_ok": ok,
        "last_result": result,
        "checked_today": try_ == today,
        "done_today": ok == today,
        "next": _prewarm_next_text(),
        "due": due["due"], "due_reason": due["reason"],
        "db_max_date": due["db_max_date"], "cache_as_of": due["cache_as_of"],
    }


def start_daily_prewarm(days: int = 120, forward: int = 5) -> bool:
    """拉起每日预热调度线程（daemon）。重复调用只启动一次。"""
    global _pw_thread
    if _pw_thread is not None and _pw_thread.is_alive():
        return False
    _pw_stop.clear()

    def _loop():
        last_checked = ""
        while not _pw_stop.is_set():
            try:
                import store as _st
                today = _dt.date.today().isoformat()
                if last_checked != today and _st.meta_get(K_PW_TRY, "") == today:
                    last_checked = today      # 重启后从 meta 恢复，不重复检查
                now = _dt.datetime.now()
                hh, mm = (int(x) for x in prewarm_at().split(":"))
                if (now.hour, now.minute) >= (hh, mm) and last_checked != today:
                    last_checked = today
                    _st.meta_set(K_PW_TRY, today)
                    if _st.meta_get(K_PW_OK, "") == today:
                        print("[eval-prewarm] 今天已预热过，跳过检查")
                        continue
                    due = prewarm_due(days, forward)
                    if not due["due"]:
                        _st.meta_set(K_PW_RESULT,
                                     f"{_now_text()} 跳过：{due['reason']}")
                        print(f"[eval-prewarm] 跳过：{due['reason']}")
                    else:
                        r = prewarm(days=days, forward=forward,
                                    why=f"每日自动（{due['reason']}）")
                        if not r.get("ok"):
                            _st.meta_set(K_PW_RESULT,
                                         f"{_now_text()} 未能启动：{r.get('note')}")
            except Exception as e:
                print(f"[eval-prewarm] 调度循环异常: {type(e).__name__}: {e}")
            _pw_stop.wait(60)      # 每分钟醒一次，比日期（同 scheduler）

    _pw_thread = threading.Thread(target=_loop, daemon=True,
                                  name="eval-prewarm-sched")
    _pw_thread.start()
    print(f"[eval-prewarm] 每日调度已启动：每天 {prewarm_at()} 检查一次"
          f"（环境变量 TICK_EVAL_PREWARM_AT 可改）")
    return True


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def selfcheck() -> Dict[str, Any]:
    """离线自检：不依赖真实历史引擎，用合成序列验证核心算法的正确性。

    这是本模块唯一能「离线验证」的部分——Slice / Spearman / IC 聚合
    都是纯函数，可以脱离数据库测试。
    """
    steps: List[Dict[str, Any]] = []

    def rec(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1. Spearman 基本性质
    rng = np.random.default_rng(42)
    x = rng.random(200)
    rec("Spearman 与自身相关 = 1",
        abs((_spearman(x, x) or 0) - 1.0) < 1e-6,
        f"{_spearman(x, x):.6f}")

    y = x * 2 + 1          # 单调变换不改变秩
    rec("Spearman 对单调变换不变",
        abs((_spearman(x, y) or 0) - 1.0) < 1e-6,
        "y = 2x + 1 应仍为 1")

    z = -x                 # 取反 → -1
    rec("Spearman 反向 = -1",
        abs((_spearman(x, z) or 0) + 1.0) < 1e-6)

    # 2. 常量序列应返回 None（无相关可言）
    c = np.ones(50)
    rec("常量序列返回 None", _spearman(c, x[:50]) is None)

    # 3. 样本过少返回 None
    rec("样本 <3 返回 None", _spearman(x[:2], y[:2]) is None)

    # 4. 完美区分的 0/1 信号应接近 1
    n = 100
    sig = np.concatenate([np.zeros(n // 2), np.ones(n // 2)])
    ret = np.concatenate([np.arange(n // 2), np.arange(n // 2) + n])
    rec("0/1 信号完美区分 → IC 接近 1",
        (_spearman(sig, ret) or 0) > 0.85,
        f"{_spearman(sig, ret):.4f}")

    # 5. _level 分级
    rec("分级 strong", _level(0.6) == "strong")
    rec("分级 mid", _level(0.4) == "mid")
    rec("分级 weak", _level(0.2) == "weak")
    rec("分级 none", _level(0.05) == "none")
    rec("分级取绝对值", _level(-0.6) == "strong", "负 ICIR 同样算 strong")

    # 6. _TruncatedEngine 截断正确性（用假引擎）
    class _FakeBase:
        def __init__(self):
            dates = [f"2026-01-{i:02d}" for i in range(1, 31)]
            self._data = {
                "TEST": {
                    "close": np.arange(30, dtype=np.float32),
                    "high": np.arange(30, dtype=np.float32),
                    "low": np.arange(30, dtype=np.float32),
                    "open": np.arange(30, dtype=np.float32),
                    "volume": np.arange(30, dtype=np.float32),
                    "amount": np.arange(30, dtype=np.float32),
                    "dates": dates,
                }
            }

    te = _TruncatedEngine(_FakeBase(), "2026-01-15")
    s = te._data.get("TEST")
    rec("截断取到目标日", (s is not None and s["dates"][-1] == "2026-01-15"),
        s["dates"][-1] if s else "无数据")
    rec("截断长度正确(15)", (s is not None and len(s["dates"]) == 15),
        str(len(s["dates"])) if s else "—")

    te2 = _TruncatedEngine(_FakeBase(), "2026-02-01")   # 超出范围
    rec("超范围截断取全部", len(te2._data["TEST"]["dates"]) == 30)

    # 7. 分层聚合的核心性质：单调递增的组收益 → 单调度接近 1
    q_inc = [0.1, 0.3, 0.5, 0.7, 0.9]
    rec("5 档单调递增 → 单调度≈1",
        (_spearman(np.arange(5, dtype=float), np.array(q_inc)) or 0) > 0.95)
    q_dec = list(reversed(q_inc))
    rec("5 档单调递减 → 单调度≈-1",
        (_spearman(np.arange(5, dtype=float), np.array(q_dec)) or 0) < -0.95)
    # 等频切分：20 个样本切 5 组，每组 4 个，组内均值正确
    vals = np.arange(20, dtype=float)      # 0..19
    chunks = [float(c.mean()) for c in np.array_split(vals, 5)]
    rec("等频 5 组均值正确", abs(chunks[0] - 1.5) < 1e-6 and abs(chunks[4] - 17.5) < 1e-6,
        str([round(v, 1) for v in chunks]))
    te3 = _TruncatedEngine(_FakeBase(), "2025-01-01")   # 早于全部
    rec("早于全部则丢弃", "TEST" not in te3._data)

    # 7. _reason_of 文案
    rec("缺字段理由非空", len(_reason_of(["turnover"])) > 0)

    # 8. 白名单与 screener 对照（能 import 才做）
    try:
        import screener
        all_keys = {d["key"] for d in screener.STRATEGY_DEFS}
        bad = [k for k in EVALUABLE_KEYS if k not in all_keys]
        rec("白名单策略都真实存在", not bad, f"无效: {bad}" if bad else f"{len(EVALUABLE_KEYS)} 个均在册")
        unknown = [k for k in UNEVALUABLE_FIELDS if k not in all_keys]
        rec("跳过名单策略都真实存在", not unknown, f"无效: {unknown}" if unknown else "全部在册")
        day_keys = {d["key"] for d in screener.STRATEGY_DEFS
                    if not d.get("intraday")}
        covered = set(EVALUABLE_KEYS) | set(UNEVALUABLE_FIELDS)
        # boll_squeeze 是候选筛选策略，作者故意不进 IC 体检（历史超额未验证），
        # 不属于「缺字段无法评估」，从「未归类」断言里剔除。
        miss = day_keys - covered - {"boll_squeeze"}
        rec("日线策略全覆盖", not miss, f"未归类: {miss}" if miss else f"{len(day_keys)} 个日线策略已分类")
    except Exception as e:
        rec("screener 对照", False, f"导入失败: {e}")

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
