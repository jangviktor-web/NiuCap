"""个股因子画像（#factor-single）：单只股票在 12 个因子上的当前值与历史分位。

与 factor_lab（池级 IC/ICIR 检验）的分工，别混：

- **factor_lab 算的是横截面统计量**：同一天把池子里几百只股票按因子排名，
  再看这个排名与未来收益排名的相关性（IC/ICIR）。它回答「按这因子排序，
  能不能选出更好的一批」。
- **单只股票没有横截面**。一只票只有一条时间序列，每期 1 个样本，IC 在
  统计上无定义（不是「算出来很差」，是「算不出来」）。所以这里**不做 IC**，
  只回答另一个问题：**这只票现在的因子值，相对它自己的历史处于什么水平**。

这正是本模块存在的理由：给个股页一个诚实、可算、可解释的画像，
而不是把池级检验硬套到单票上制造看似专业的噪声。

设计取舍：
1. **分位基准是本股自身历史，不是池内排名。** 单票可得、语义清楚
   （「它比过去 3 年 72% 的日子更波动」）。池内排名在个股页意义有限，
   且要求当天全市场快照，代价高。
2. **完全复用 factor_lab.build_factors。** 那批 _f_* 函数都是按列运算，
   N=1 时照常工作，口径与因子实验室严格一致——两处数字打架是最难查的 bug。
3. **样本不足就明说。** 有效历史少于 MIN_HIST 个点时 pct=None，
   前端显示「样本不足」，不编一个分位糊弄人。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

# 分位基准要多少历史点才可信。250 ≈ 一年交易日：少于一年，「历史分位」
# 很容易被一小段行情主导（比如单边下跌半年，波动率分位必然虚高）。
MIN_HIST = 250

# 取多少历史点算分位基准。750 ≈ 三年，够覆盖一轮小周期，又不至于把
# 风格完全不同的陈年行情算进来。
HIST_DAYS = 750

# 五档标签（按分位，从低到高）。用「偏低/偏低偏」这类措辞是刻意的：
# 分位高低本身没有好坏，好坏取决于因子方向（低波动好还是高波动好），
# 这里只描述「位置」，不替用户下结论。
_LEVELS = [(0.2, "很低"), (0.4, "偏低"), (0.6, "中等"), (0.8, "偏高")]


def _level(pct: Optional[float]) -> str:
    if pct is None:
        return "样本不足"
    for edge, name in _LEVELS:
        if pct < edge:
            return name
    return "很高"


# 少数因子的「分位高」容易被读反，单独给一套措辞：
# mdd_60 是负值（-9%），分位 79% 的含义是「回撤比历史上 79% 的时候更浅」，
# 直接标「偏高」会让人以为「回撤偏大」，正好读反。这里把话说全。
_LEVEL_OVERRIDE = {
    "mdd_60": {"很低": "回撤很深", "偏低": "回撤较深", "中等": "回撤中等",
               "偏高": "回撤较浅", "很高": "回撤很浅"},
    "vol_surge": {"很低": "缩量明显", "偏低": "略微缩量", "中等": "量能常态",
                  "偏高": "略微放量", "很高": "显著放量"},
}


def _level_for(key: str, pct: Optional[float]) -> str:
    lv = _level(pct)
    return _LEVEL_OVERRIDE.get(key, {}).get(lv, lv)


def _load_mats(code: str, days: int = HIST_DAYS) -> Dict[str, Any]:
    """读单只股票日线，构造成 factor_lab 认的 (T, 1) 矩阵。

    用 store.bars_daily（升序、含 OHLCV+amount），不经 datasource 联网——
    个股页只需要库内已有日线，不该为了画个画像去拉分钟线。
    """
    import store

    store.initialize()
    rows = store.get_bars(code, limit=days)
    if not rows:
        raise ValueError(f"库内无 {code} 的日线数据")

    dates = [str(r["date"]) for r in rows]
    fields = ("open", "close", "high", "low", "volume", "amount")
    T = len(dates)
    mats: Dict[str, Any] = {"dates": dates, "codes": [code]}
    for k in fields:
        col = np.full((T, 1), np.nan, dtype=np.float64)
        for i, r in enumerate(rows):
            try:
                v = r.get(k)
                col[i, 0] = float(v) if v is not None else np.nan
            except (TypeError, ValueError):
                pass
        mats[k] = col
    return mats


def _pct_rank(series: np.ndarray, value: float) -> Optional[float]:
    """value 在 series 有效值中的分位（0~1）。有效点不足 MIN_HIST 返回 None。

    口径：**严格小于的比例 + 平局折半**。这是分位的标准写法（不把自己算进
    「超过自己」里），最小值因此落在 0 而不是 1/N。早年用「<=」会让最小值
    也带 0.4% 分位，且重复值多时（如成交额对数、RSI 常见整数堆积）分位被
    系统性抬高——同一只股票的分位在两次计算间不该无故跳动。
    """
    s = series[np.isfinite(series)]
    if s.size < MIN_HIST:
        return None
    less = np.count_nonzero(s < value)
    equal = np.count_nonzero(s == value)
    return float((less + equal * 0.5) / s.size)


def profile(code: str, days: int = HIST_DAYS) -> Dict[str, Any]:
    """算一只股票的因子画像。返回可直接渲染的 dict。

    结构：
      {ok, code, name, days, span:[d0,d1], items:[{key,name,cat,value,pct,level,note}],
       coverage:{有值因子数, 总因子数}}
    """
    import factor_lab as flm

    mats = _load_mats(code, days)
    dates: List[str] = mats["dates"]
    if len(dates) < 60:
        raise ValueError(f"日线仅 {len(dates)} 根，不足以算因子（需 ≥60）")

    facs = flm.build_factors(mats)
    items: List[Dict[str, Any]] = []
    for key, name, cat, _fn in flm.FACTOR_DEFS:
        fmat = facs.get(key)
        if fmat is None:
            continue
        col = fmat[:, 0]
        # 当前值取**最后一个有效点**：当日若停牌/新股，末格会是 NaN，
        # 直接取 [-1] 会让整个因子莫名空缺。
        valid = np.flatnonzero(np.isfinite(col))
        if valid.size == 0:
            items.append({"key": key, "name": name, "cat": cat,
                          "value": None, "pct": None, "level": "样本不足",
                          "note": "该因子在这只票上算不出有效值"})
            continue
        last_i = int(valid[-1])
        value = float(col[last_i])
        pct = _pct_rank(col[:last_i + 1], value)
        items.append({
            "key": key, "name": name, "cat": cat,
            "value": value, "pct": pct, "level": _level_for(key, pct),
            "at": dates[last_i] if last_i < len(dates) else "",
            "note": "",
        })

    have = sum(1 for x in items if x["value"] is not None)
    return {
        "ok": True, "code": code, "days": len(dates),
        "span": [dates[0], dates[-1]],
        "items": items,
        "coverage": {"have": have, "total": len(items)},
        "_pool": mats.get("_pool"),
    }


# ---------------------------------------------------------------------------
# 缓存：个股页同代码反复进，同名股票没必要重算（纯内存计算，但仍要读库）
# ---------------------------------------------------------------------------
import time as _time

_CACHE: Dict[str, Any] = {}
_TTL = 600.0     # 10 分钟：日线日更，画像一天内基本不变


def profile_cached(code: str, days: int = HIST_DAYS) -> Dict[str, Any]:
    hit = _CACHE.get(code)
    if hit and (_time.time() - hit[0]) < _TTL:
        return hit[1]
    out = profile(code, days)
    _CACHE[code] = (_time.time(), out)
    return out


def cache_clear() -> None:
    _CACHE.clear()


def selfcheck() -> int:
    """离线自检：分位档位、NaN 处理、样本不足、真实库内个股（若可用）。"""
    fails = 0

    def ok(label, cond, extra=""):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {label}{(' -> ' + str(extra)) if extra else ''}")
        if not cond:
            fails += 1

    ok("_level：0 分位 → 很低", _level(0.0) == "很低")
    ok("_level：0.5 分位 → 中等", _level(0.5) == "中等")
    ok("_level：0.99 分位 → 很高", _level(0.99) == "很高")
    ok("_level：None → 样本不足", _level(None) == "样本不足")
    ok("_level：边界 0.2 → 偏低（含）", _level(0.2) == "偏低")

    # 方向易读反的因子：分位高 ≠ 差。mdd_60 分位 0.79 是「回撤比历史更浅」，
    # 绝不能显示成「偏高」（用户会读成回撤偏大）。
    ok("_level_for：回撤高分位 → 回撤较浅",
       _level_for("mdd_60", 0.79) == "回撤较浅", _level_for("mdd_60", 0.79))
    ok("_level_for：回撤低分位 → 回撤很深",
       _level_for("mdd_60", 0.05) == "回撤很深")
    ok("_level_for：量比高分位 → 显著放量",
       _level_for("vol_surge", 0.95) == "显著放量")
    ok("_level_for：未覆盖的因子走通用档位",
       _level_for("mom_20", 0.79) == "偏高")
    ok("_level_for：样本不足不被覆盖改写",
       _level_for("mdd_60", None) == "样本不足")

    # 分位：样本不足返回 None
    ok("_pct_rank：样本不足 → None",
       _pct_rank(np.arange(10, dtype=float), 5.0) is None)

    # 分位：全量有效。口径含「平局折半」，故端点值各带半个样本的偏移
    # （最小 0.5/N、最大 1-0.5/N），断言按这个容差写。
    s = np.arange(MIN_HIST, dtype=float)
    half = 0.5 / MIN_HIST
    ok("_pct_rank：最小值 → 分位≈0", abs(_pct_rank(s, 0.0) - half) < 1e-9,
       _pct_rank(s, 0.0))
    ok("_pct_rank：最大值 → 分位≈1",
       abs(_pct_rank(s, float(MIN_HIST - 1)) - (1.0 - half)) < 1e-9)

    # 分位：大量重复值时不应被抬到 1（平局折半）
    s_dup = np.concatenate([np.zeros(200), np.arange(MIN_HIST - 200, dtype=float)])
    ok("_pct_rank：最大重复值 → 分位远小于 1",
       _pct_rank(s_dup, 0.0) < 0.6)

    # 分位：含 NaN 不影响
    s2 = np.concatenate([np.arange(MIN_HIST, dtype=float), [np.nan] * 50])
    ok("_pct_rank：NaN 被剔除", abs(_pct_rank(s2, 0.0) - half) < 1e-9)

    # 真实数据（库里有就跑）
    try:
        import store
        store.initialize()
        row = store._conn().execute(
            "SELECT code, COUNT(*) AS n FROM daily_bars"
            " GROUP BY code HAVING n >= 300 ORDER BY n DESC LIMIT 1").fetchone()
    except Exception as e:
        row = None
        print(f"  (跳过真实数据测试：{type(e).__name__})")
    if row:
        code = str(row["code"])
        try:
            p = profile(code)
            ok(f"profile({code})：返回 12 个因子", len(p.get("items", [])) == 12,
               len(p.get("items", [])))
            ok("profile：每个因子有 level", all(x.get("level") for x in p["items"]))
            ok("profile：覆盖数不超总数",
               p["coverage"]["have"] <= p["coverage"]["total"])
            vals = [x["value"] for x in p["items"] if x["value"] is not None]
            ok("profile：因子值均为有限数", all(np.isfinite(vals)))
            pcts = [x["pct"] for x in p["items"] if x["pct"] is not None]
            ok("profile：分位都在 0~1", all(0.0 <= v <= 1.0 for v in pcts))
        except Exception as e:
            ok(f"profile({code}) 不抛异常", False, f"{type(e).__name__}: {e}")
    else:
        print("  (库内无 ≥300 根日线的股票，跳过真实数据测试)")

    # 空代码应抛 ValueError 而不是崩
    try:
        profile("sh999999")
        ok("不存在的代码 → ValueError", False)
    except ValueError:
        ok("不存在的代码 → ValueError", True)
    except Exception as e:
        ok("不存在的代码 → ValueError", False, type(e).__name__)

    print(f"factor_single selfcheck: {'OK' if fails == 0 else str(fails) + ' 项失败'}")
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if selfcheck() else 0)
