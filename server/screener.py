"""
内置策略选股引擎 —— 借鉴 TSP(tick-stock-panel) 的「内置策略 + 快照扫描」思路

设计要点：
  - 每个策略是一个纯函数：接收全市场快照 list[dict]，返回命中的 code 集合
  - 快照扫描保证秒级响应；需要【历史序列】的策略再叠加 history 引擎的指标
  - 分类对齐 TSP：趋势/形态 · 量价/涨停 · 反转/波动
  - 每个策略带 META（名称/说明/分类/参数），前端可点选扫描

## 关于「伪历史策略」（重要）

有 6 个策略的名字要求历史数据，但早期实现只用当日快照字段近似，
导致「名字承诺的」与「实际算的」不一致。现已改造为真算：

    s_breakout_20h     突破20日新高   → 用 HHV(H,20) 真实比较
    s_ma_bull          均线多头       → 用 MA5/10/20/60 真实排列
    s_platform_break   平台突破       → 用前 60 日真实振幅 + 突破前高
    s_volume_surge     放量突破       → 用「当日量 / 前20日均量」相对量比
    s_oversold_rebound 超跌反弹       → 用真实 N 日跌幅 + 企稳
    s_pullback_ma20    回踩企稳       → 用真实 MA20 偏离度 + 缩量

改造后这些策略的签名多了一个可选参数 `hist`（history.HistoryEngine）：

    fn(rows, hist=engine)   # 传了 hist 就算真实指标；不传则返回空集
                            # （而不是悄悄退化成旧的伪实现 —— 宁可不出结果，
                            #   也不能给出与名字不符的错误结果）

指标正确性由 `history.py` 的自检 + pandas 交叉验证保证，
详见 `docs/策略改造难点评估.md`。

快照字段（datasource.pool_rows 产出）：
  code, symbol, name, price, change_pct, pe, pb, total_cap, float_cap,
  turnover, amount, volume, open, high, low, prev_close, pools
"""

import math

import numpy as np  # 经典指标策略里用到 np（如 cmf_breakout 的 20 日新高）

# 经典指标（方案 A · 从 cinar/indicator 蒸馏）：ATR / SuperTrend / ConnorsRSI /
# TD9 / MFI / CMF 的纯 numpy 实现。screener 的新策略从这里取「当日值」，
# 而非往 history.metrics 塞（保持 history.py 稳定）。
from indicators_extra import (       # noqa: E402
    supertrend_flip_last, atr_last, connors_rsi_last,
    td_count_last, mfi_last, cmf_last, bollinger_squeeze_last,
)
from mytt import KDJ                # noqa: E402  （J 值异常过滤用）

# 板块前缀判定（用于涨跌停幅度）
def _limit_pct(code: str) -> float:
    """按板块返回涨停幅度（%）。主板10，创业板/科创板20，北交所30，ST需另判。

    302 是创业板的预留代码段，新股会陆续启用，必须与 300/301 同样按 20% 处理。
    """
    c = code.replace("sh", "").replace("sz", "").replace("bj", "")
    if code.startswith("bj"):
        return 30.0
    if c.startswith(("300", "301", "302", "688", "689")):
        return 20.0
    return 10.0


def _is_st(name: str) -> bool:
    return ("ST" in name.upper()) or ("*ST" in name.upper())


def _lx(sym: str) -> str:
    """市场前缀 + 6 位代码"""
    d = "".join(ch for ch in sym if ch.isdigit())
    return d


# ===========================================================================
# 工具：把 list[dict] 快速转成便于计算的形态
# ===========================================================================

def _num(v, default=0.0):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


# ===========================================================================
# 历史指标接入
# ===========================================================================

def _hist_of(kw):
    """从策略参数里取 history 引擎。

    没传就返回 None —— 此时所有「需要历史」的策略都返回空集，
    这是刻意的：宁可不出结果，也不给出与策略名字不符的错误结果。
    """
    return kw.get("hist")


def _metrics(hist, code, need=None):
    """取某只股票的指标；数据不足 / 无引擎时返回 None。"""
    if hist is None:
        return None
    try:
        return hist.metrics(code, need=need)
    except Exception:
        return None


# ---- 边界比较（吸收 float32 表示误差，见 history.EPS 的说明）----

_EPS = 1e-5


def _gt(a, b) -> bool:
    """a 显著大于 b。用于突破 / 新高 / 站上均线。

    序列以 float32 存储，5.10 会变成 5.09999990。若直接用 `a > b`，
    「当日最高 5.10 平前高 5.10」会被误判为「突破」。实测破新高命中
    450 只里有 6 只属于这种噪声，本函数将其剔除。
    """
    if a is None or b is None:
        return False
    try:
        a = float(a); b = float(b)
    except (TypeError, ValueError):
        return False
    return a > b * (1.0 + _EPS) if b != 0 else a > 0


def _lt(a, b) -> bool:
    """a 显著小于 b。用于创新低 / 跌破。"""
    if a is None or b is None:
        return False
    try:
        a = float(a); b = float(b)
    except (TypeError, ValueError):
        return False
    return a < b * (1.0 - _EPS) if b != 0 else a < 0


def _ge(a, b) -> bool:
    """a 大于或等于 b（带容差）。用于「价格站上均线」。

    注意这里**允许相等**（浮点意义上略小一点点也算站上）。
    原因：快照的 price 是 float64 原始精度，而均线由 float32 历史序列
    算出，两边精度不同。例如真实价格 5.26，均线算出来是 5.26000004，
    用 `price >= ma` 会误判为「未站上」。实测有 1 只因此漏掉。
    """
    if a is None or b is None:
        return False
    try:
        a = float(a); b = float(b)
    except (TypeError, ValueError):
        return False
    return a >= b * (1.0 - _EPS) if b != 0 else a >= 0


# ---------------------------------------------------------------------------
# 策略参数取用（走查回测调参的基础设施）
# ---------------------------------------------------------------------------

def _p(kw, name: str, default: float) -> float:
    """从策略调用参数里取一个可调阈值，缺省时用默认值。

    存在意义：这些阈值原本是散落在各策略函数里的硬编码魔法数字
    （如 `35.0`、`0.85`），无法在**不修改源码**的前提下调参，
    也就没法做走查回测（训练段调参 → 测试段验证）。

    现在统一改成「默认值 = 原来的硬编码值」，调用方可通过
    `run_strategies(..., params={"max_range60": 30.0})` 覆盖。

    不传参数时返回值与改造前**完全一致**，因此不影响现有 API 与前端。

    容错：传入 None / 非数字 / 无法转换时一律回落到默认值，
    而不是抛异常——扫描是全市场批量操作，不应因单个坏参数而全盘失败。
    """
    if not kw:
        return default
    v = kw.get(name)
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    # NaN / inf 视为无效，回落默认值
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def default_params(key: str) -> dict:
    """返回某个策略的默认可调参数。

    供走查回测生成参数网格、以及前端展示「本策略有哪些参数可调」。
    未登记的策略返回空字典（表示该策略没有可调阈值）。
    """
    return dict(PARAM_DEFAULTS.get(key) or {})


# 各策略的可调参数默认值。这里的数值就是参数化之前代码里的硬编码值，
# 必须保持一一对应——它是「不传参数时结果不变」这条兼容性承诺的依据。
PARAM_DEFAULTS = {
    "breakout_20h":     {"hold_ratio": 0.98},
    "platform_break":   {"max_range60": 35.0, "hold_ratio": 0.98,
                         "min_vol_ratio": 1.2, "min_chg": 2.0},
    "volume_surge":     {"min_vol_ratio": 2.0, "min_chg": 3.0,
                         "min_amount": 5000.0},
    "oversold_rebound": {"max_chg20": -15.0, "min_chg": 1.0, "max_chg": 6.0},
    "pullback_ma20":    {"max_bias": 2.0, "max_vol_ratio": 0.85,
                         "min_chg": -3.0},
    "pattern_score":    {"min_score": 85.0},
    # ma_bull 是纯结构判定（均线相对位置），没有可调阈值
    "ma_bull":          {},
    # ---- 方案 A · 经典指标（默认值 = 蒸馏源码的默认阈值）----
    "supertrend_long":  {"mult": 3.0},
    "atr_breakout":     {"break_mult": 2.0},
    "connors_rsi_dip":  {"crsi_th": 15.0},
    "td9_buy":          {},
    "mfi_oversold":     {"mfi_th": 20.0},
    "cmf_breakout":     {"cmf_th": 0.1},
    "boll_squeeze":     {"squeeze_pct": 10.0},
}


# ===========================================================================
# 策略实现
# 每个函数签名： fn(rows, **params) -> set[code]
# ===========================================================================

# ---------- 趋势 / 形态 ----------

def s_breakout_20h(rows, **kw):
    """突破 20 日新高。

    真实语义：**当日最高价**创下**前 20 个交易日的新高**。

    改造前是「当日 high >= 昨收×1.095 且涨幅>5%」——只看昨日一天，
    与「20 日新高」毫无关系（涨幅 5% 也不等于突破 20 日高点）。

    现在的判定：
      1. 当日 high > 前 20 日最高价（不含当日，用 HHV(H,20)）
      2. 当日收盘仍在突破位附近（收于前高之上或至少 hold_ratio，避免冲高回落）
      3. 需要至少 21 根历史数据

    可调参数：hold_ratio（默认 0.98）
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    hold = _p(kw, "hold_ratio", 0.98)
    for r in rows:
        code = r["code"]
        m = _metrics(hist, code, need=["hhv20", "ma20"])
        if not m or m.get("hhv20") is None:
            continue
        hhv20 = m["hhv20"]
        high = _num(r["high"]); price = _num(r["price"]); prev = _num(r["prev_close"])
        if high <= 0 or prev <= 0 or hhv20 <= 0:
            continue
        # 当日最高突破前 20 日高点（带容差：平前高不算突破，见 history.EPS）
        if not _gt(high, hhv20):
            continue
        # 收盘不显著回落（收在突破位 hold_ratio 以上）
        if price < hhv20 * hold:
            continue
        out.add(code)
    return out


def s_ma_bull(rows, **kw):
    """均线多头排列。

    真实语义：**MA5 > MA10 > MA20 > MA60**，短中期均线依次向上排列。

    改造前是「涨幅 2%~8% + 换手 1%~15%」——**压根没算任何均线**，
    实测命中了全市场 30%（1120/3761），几乎等于没筛选。

    现在的判定：
      1. MA5 > MA10 > MA20 > MA60（严格多头排列）
      2. 收盘价在 MA5 之上（趋势仍在延续，而非刚跌破）
      3. 四条均线都要算得出（需至少 60 根历史）
      4. 成交额不为 0（排除停牌）
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    for r in rows:
        code = r["code"]
        m = _metrics(hist, code, need=["ma5", "ma10", "ma20", "ma60", "ma_align"])
        if not m:
            continue
        ma5, ma10, ma20, ma60 = (m.get("ma5"), m.get("ma10"),
                                 m.get("ma20"), m.get("ma60"))
        if None in (ma5, ma10, ma20, ma60):
            continue                     # 历史不足 60 根，不参与判断
        # 严格多头排列：每条短均线都要「显著」高于长均线（带容差，
        # 否则 float32 下两条几乎相等的均线会被判成「多头排列」）
        if not (_gt(ma5, ma10) and _gt(ma10, ma20) and _gt(ma20, ma60)):
            continue
        price = _num(r["price"])
        if price <= 0 or not _ge(price, ma5):
            continue                      # 价格须站上 MA5（带容差）
        if _num(r["amount"]) <= 0:
            continue
        out.add(code)
    return out


def s_vol_price_up(rows, **kw):
    """量价齐升：涨幅 3%+ 且换手 3%+（量能配合）"""
    out = set()
    for r in rows:
        cp = _num(r["change_pct"]); to = _num(r["turnover"])
        if cp >= 3.0 and to >= 3.0:
            out.add(r["code"])
    return out


def s_platform_break(rows, **kw):
    """平台整理后突破。

    真实语义：**前期横盘整理（振幅小）→ 当日放量突破平台上沿**。

    改造前是「涨幅 4%~9.9% + 非一字板 + 换手≥2%」——**完全没看前 60 日
    是否横盘**，只要当天涨得够多就算，实测命中 399 只（10.6% 全市场）。

    现在的判定：
      1. 前 60 日振幅 ≤ max_range60%（横盘整理充分；区间最高/最低比值）
      2. 当日最高价突破前 60 日高点
      3. 收盘站稳在平台上沿附近（≥ 前高的 hold_ratio）
      4. 量能配合：当日量 ≥ 前 20 日均量的 min_vol_ratio 倍（突破需放量确认）
      5. 涨幅 ≥ min_chg%（排除一字板）

    可调参数：max_range60（默认 35.0）、hold_ratio（0.98）、
              min_vol_ratio（1.2）、min_chg（2.0）

    ## 振幅阈值 35% 的选取依据（实测敏感性）

        振幅上限   命中数(3761只池)   占比    命中名单振幅中位
        ≤20%        5              0.13%     15.1%
        ≤25%       15              0.40%     21.3%
        ≤30%       30              0.80%     25.0%
        ≤35%       36              0.96%     25.9%   ← 采用
        ≤40%       49              1.30%     27.9%
        ≤45%       58              1.54%     33.5%
        ≤50%       66              1.75%     34.8%

    选 35% 而非更宽，理由：
      (a) 「平台」的核心语义就是**横盘**。命中名单的振幅中位仅 25.9%，
          说明真实平台突破的振幅天然远离 35% —— 这个阈值并未卡在边缘，
          而是留出了充足余量。
      (b) 放宽到 45% 只多 22 只（+0.6%），但这 22 只的振幅集中在 36%~43%，
          已属「宽幅震荡」而非「平台」，会稀释策略纯度。
      (c) 反向验证：35%~45% 区间的 22 只「准命中」中，多数距前高仅 +0.5%~+2%，
          形态上是「贴着上沿的宽幅震荡」而非「突破平台」，语义不符。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    max_rng = _p(kw, "max_range60", 35.0)
    hold = _p(kw, "hold_ratio", 0.98)
    min_vr = _p(kw, "min_vol_ratio", 1.2)
    min_cp = _p(kw, "min_chg", 2.0)
    for r in rows:
        code = r["code"]
        m = _metrics(hist, code, need=["hhv60", "range60_pct", "vol_ratio"])
        if not m:
            continue
        hhv60 = m.get("hhv60")
        rng = m.get("range60_pct")
        vr = m.get("vol_ratio")
        if hhv60 is None or rng is None or vr is None:
            continue                     # 历史不足 60 根，不参与判断
        if rng > max_rng:
            continue                     # 不是横盘，是趋势或宽幅震荡
        high = _num(r["high"]); price = _num(r["price"])
        op = _num(r["open"])
        if high <= 0 or price <= 0:
            continue
        if not _gt(high, hhv60):
            continue                     # 未突破平台上沿（带容差）
        if price < hhv60 * hold:
            continue                     # 冲高回落，不算站稳
        if vr < min_vr:
            continue                     # 无量突破不可信
        if op >= high:
            continue                     # 一字板
        cp = _num(r["change_pct"])
        if cp < min_cp:
            continue
        out.add(code)
    return out


# ---------- 量价 / 涨停 ----------

def s_limit_up(rows, **kw):
    """涨停股：当日封板（涨幅 ≈ 板块涨停幅度）"""
    out = set()
    for r in rows:
        cp = _num(r["change_pct"]); code = r["code"]
        lim = _limit_pct(code)
        # ST 股票 5% 涨停
        if _is_st(r["name"]):
            lim = 5.0
        if cp >= lim - 0.6:
            out.add(code)
    return out


def s_near_limit(rows, **kw):
    """接近涨停：涨幅在涨停幅度的 70%~99%（打板预备队）"""
    out = set()
    for r in rows:
        cp = _num(r["change_pct"]); code = r["code"]
        lim = 5.0 if _is_st(r["name"]) else _limit_pct(code)
        if lim * 0.70 <= cp < lim - 0.6:
            out.add(code)
    return out


def s_strong_turnover(rows, **kw):
    """高换手强势：换手 8%+ 且上涨 2%+"""
    out = set()
    for r in rows:
        if _num(r["turnover"]) >= 8.0 and _num(r["change_pct"]) >= 2.0:
            out.add(r["code"])
    return out


def s_volume_surge(rows, **kw):
    """放量突破。

    真实语义：**成交量相对自身近期均量显著放大**，同时价格上行。

    改造前是「成交额 ≥ 3 亿 且涨幅 ≥ 3%」——这是**绝对额门槛**，
    对大盘股等于没门槛（茅台天天几十亿），对小盘股又过高，
    完全没有体现「放量」的相对含义。

    现在的判定：
      1. 当日量 / 前 20 日均量 ≥ min_vol_ratio（真正的相对放量）
      2. 涨幅 ≥ min_chg%
      3. 成交额 ≥ min_amount 万元（过滤掉流动性过差的，避免虚假放量）
      4. 前 20 日均量有效（需至少 21 根历史）

    可调参数：min_vol_ratio（默认 2.0）、min_chg（3.0）、min_amount（5000.0）
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    min_vr = _p(kw, "min_vol_ratio", 2.0)
    min_cp = _p(kw, "min_chg", 3.0)
    min_amt = _p(kw, "min_amount", 5000.0)
    for r in rows:
        code = r["code"]
        m = _metrics(hist, code, need=["vol_ratio"])
        if not m or m.get("vol_ratio") is None:
            continue
        if m["vol_ratio"] < min_vr:
            continue
        if _num(r["change_pct"]) < min_cp:
            continue
        if _num(r["amount"]) < min_amt:   # 单位：万元
            continue
        out.add(code)
    return out


def s_limit_up_momentum(rows, **kw):
    """涨停动量：涨停 或 大涨(7%+)且高换手"""
    out = set()
    for r in rows:
        cp = _num(r["change_pct"]); code = r["code"]
        lim = 5.0 if _is_st(r["name"]) else _limit_pct(code)
        if cp >= lim - 0.6 or (cp >= 7.0 and _num(r["turnover"]) >= 5.0):
            out.add(code)
    return out


# ---------- 反转 / 波动 ----------

def s_oversold_rebound(rows, **kw):
    """超跌反弹。

    真实语义：**前期深跌 → 当日出现企稳反弹**。

    改造前是「涨 1%~6% + 换手 ≥4%」——**完全没看前期跌幅**，
    「超跌」二字名存实亡。

    现在的判定：
      1. 前 20 日跌幅 ≥ |max_chg20|%（真实超跌，用区间涨跌幅算）
      2. 当日上涨 min_chg%~max_chg%（反弹初期，尚未追高）
      3. 当日最低价未创新低（不再破位，有企稳迹象）
      4. 成交额不为 0

    可调参数：max_chg20（默认 -15.0）、min_chg（1.0）、max_chg（6.0）
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    max_c20 = _p(kw, "max_chg20", -15.0)
    min_cp = _p(kw, "min_chg", 1.0)
    max_cp = _p(kw, "max_chg", 6.0)
    for r in rows:
        code = r["code"]
        m = _metrics(hist, code, need=["chg20", "llv20"])
        if not m:
            continue
        chg20 = m.get("chg20")
        llv20 = m.get("llv20")
        if chg20 is None or llv20 is None:
            continue                     # 历史不足，不参与判断
        if chg20 > max_c20:
            continue                     # 跌得不够多，不算超跌
        cp = _num(r["change_pct"])
        if not (min_cp <= cp <= max_cp):
            continue
        low = _num(r["low"])
        if low <= 0 or _lt(low, llv20):
            continue                     # 当日还在创新低，没企稳（带容差）
        if _num(r["amount"]) <= 0:
            continue
        out.add(code)
    return out


def s_low_vol_leader(rows, **kw):
    """低波动稳健：振幅小（当日 high-low 小），小幅上涨，中大市值"""
    out = set()
    for r in rows:
        price = _num(r["price"]); hi = _num(r["high"]); lo = _num(r["low"])
        prev = _num(r["prev_close"]); cap = _num(r["total_cap"])
        if price <= 0 or prev <= 0 or hi <= 0 or lo <= 0:
            continue
        amp = (hi - lo) / prev * 100.0
        cp = _num(r["change_pct"])
        if amp <= 2.5 and 0 < cp <= 3.0 and cap >= 100:
            out.add(r["code"])
    return out


def s_pullback_ma20(rows, **kw):
    """回踩 MA20 企稳。

    真实语义：**上升趋势中回踩 20 日均线，缩量止跌企稳**。

    改造前是「涨跌幅 -2%~+2% + 换手 1%~8%」——**既没算 MA20，
    也没判断是否回踩**，等于随机选了一批当天没怎么动的股票。

    现在的判定：
      1. 中期趋势向上：MA20 > MA60（确保是「上升途中的回踩」）
      2. 价格贴近 MA20：偏离度在 -max_bias% ~ +max_bias% 之间
      3. 明显缩量：当日量 / 前 20 日均量 ≤ max_vol_ratio（回踩必须缩量才叫企稳）
      4. 当日跌幅不超过 |min_chg|%（未破位）
      5. 成交额不为 0

    可调参数：max_bias（默认 2.0）、max_vol_ratio（0.85）、min_chg（-3.0）

    阈值说明：这两个参数经敏感性测试确定（见下），偏离 ±3%+量比≤1.0
    会放过 22% 的股票，失去筛选意义；±1.5%+量比≤0.8 过严（8.1%）。
    取 ±2% + 量比≤0.85 约命中 10%，与「回踩企稳」的语义相符。

        偏离区间   量比上限   命中占比
        ±1.5%     ≤0.8      8.1%
        ±2%       ≤0.85    ~10%     ← 采用
        ±3%       ≤1.0      22.0%
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    max_bias = _p(kw, "max_bias", 2.0)
    max_vr = _p(kw, "max_vol_ratio", 0.85)
    min_cp = _p(kw, "min_chg", -3.0)
    for r in rows:
        code = r["code"]
        m = _metrics(hist, code,
                     need=["ma20", "ma60", "bias_ma20", "vol_ratio"])
        if not m:
            continue
        ma20, ma60 = m.get("ma20"), m.get("ma60")
        bias, vr = m.get("bias_ma20"), m.get("vol_ratio")
        if None in (ma20, ma60, bias):
            continue                     # 历史不足 60 根，不参与判断
        if not _gt(ma20, ma60):
            continue                     # 中期趋势不向上，谈不上「回踩」（带容差）
        if not (-max_bias <= bias <= max_bias):
            continue                     # 没贴近 MA20
        if vr is not None and vr > max_vr:
            continue                     # 未明显缩量，不是企稳
        cp = _num(r["change_pct"])
        if cp < min_cp:
            continue                     # 破位
        if _num(r["amount"]) <= 0:
            continue
        out.add(code)
    return out


def s_reversal_hammer(rows, **kw):
    """长下影反击：长下影（下影 > 实体 2 倍），收盘转涨"""
    out = set()
    for r in rows:
        op = _num(r["open"]); cl = _num(r["price"])
        hi = _num(r["high"]); lo = _num(r["low"]); prev = _num(r["prev_close"])
        if min(op, cl, hi, lo) <= 0 or prev <= 0:
            continue
        body = abs(cl - op)
        lower_shadow = min(op, cl) - lo
        upper_shadow = hi - max(op, cl)
        if lower_shadow >= max(body, 0.01) * 2 and lower_shadow > upper_shadow and cl >= prev:
            out.add(r["code"])
    return out


def s_gap_up(rows, **kw):
    """跳空高开强势：开盘涨幅 2%+ 且收盘不回落（收盘 > 开盘）"""
    out = set()
    for r in rows:
        op = _num(r["open"]); cl = _num(r["price"]); prev = _num(r["prev_close"])
        if prev <= 0 or op <= 0:
            continue
        gap = (op - prev) / prev * 100.0
        if gap >= 2.0 and cl >= op:
            out.add(r["code"])
    return out


def s_small_cap_active(rows, **kw):
    """小市值活跃：总市值 < 100亿，换手 5%+，上涨"""
    out = set()
    for r in rows:
        cap = _num(r["total_cap"])
        if 0 < cap < 100 and _num(r["turnover"]) >= 5.0 and _num(r["change_pct"]) > 0:
            out.add(r["code"])
    return out


# ---------- 形态相似度（打分型，P1） ----------

def s_pattern_score(rows, **kw):
    """形态评分：按实测的有利形态方向打分，取高分。

    与其它策略的区别：**这是打分型而非判定型**。其它 15 个策略对每只股票
    输出「符合/不符合」，这里输出 0~100 分，按门槛筛选。

    评分维度与方向**来自本项目实测**（167,721 个观察点，见
    docs/形态匹配说明.md），不是照搬参考项目的经验权重：

        区间位置  （越低越好，权重 0.27）最低分位胜率 58.5% vs 最高 46.3%
        量能比    （越低越好，权重 0.28）56.6% vs 44.3% —— 放量反而危险
        前60日涨幅（越小越好，权重 0.34）55.6% vs 40.5%
        前60日振幅（越小越好，权重 0.11）区分度较弱故权重低

    样本外效果：分数 ≥85 组未来 20 日均值 +2.93%、胜率 57.8%；
               分数 <55 组 均值 -0.07%、胜率 41.9%。

    可调参数：min_score（默认 85）

    注意：**原 B1 方案（找与历史大涨股形态相似的票）经实测被否定**，
    高相似度组的未来收益反而略低于低相似度组。详见 docs/形态匹配说明.md
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    min_score = _p(kw, "min_score", 85.0)
    try:
        import similarity as sim
    except Exception:
        return out

    for r in rows:
        code = r["code"]
        s = hist._data.get(code)
        if not s:
            continue
        try:
            res = sim.score_series_at(s, len(s["close"]) - 1)
        except Exception:
            continue
        if not res:
            continue
        sc, _parts = res
        if sc >= min_score:
            out.add(code)
    return out


# ===========================================================================
# 经典指标策略（方案 A · 蒸馏自 cinar/indicator v2.1.44）
#
# 与上面 16 个策略一样，都是「纯函数 + 需历史序列（hist=True）」。区别是它们
# 的信号来自经典技术指标（而非形态/量价的直觉规则）。数据源只有 OHLCV +
# amount —— 全部来自 daily_bars，因此「每日收盘可重建」→ 自动进体检白名单
# → 分层测试 → 模拟净值，全链路可验证。
#
# 指标算法全部在 indicators_extra.py（纯 numpy，带 selfcheck）。这里只做
# 「取序列 → 调指标 → 边界判定」三件事，保持策略层薄。
# ===========================================================================

def _series(hist, code):
    """取某只股票的原始 OHLCV 序列（真实引擎 / 截断引擎通用）。

    真实引擎与策略体检的 `_TruncatedEngine` 都暴露 `series(code)`；
    个别环境若没有该方法，退化到 `_data.get`。返回 None 表示无数据。
    """
    if hist is None:
        return None
    try:
        s = hist.series(code)
        if s is not None:
            return s
    except Exception:
        pass
    try:
        return hist._data.get(code)
    except Exception:
        return None


def s_supertrend_long(rows, **kw):
    """超级趋势翻多：SuperTrend 当日由空翻多。

    蒸馏源：volatility/super_trend.go。中线 = (H+L)/2，通道 = ±3×ATR(10)，
    收盘上穿上轨翻多、下穿下轨翻空。取「翻多当日」作为买点 —— 即趋势状态
    从 −1 变为 +1 的那一根（经典 SuperTrend 买点，而非单纯「处于多头」）。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    mult = _p(kw, "mult", 3.0)
    for r in rows:
        s = _series(hist, r["code"])
        if not s:
            continue
        flipped = supertrend_flip_last(
            s["high"], s["low"], s["close"], period=10, mult=mult)
        if flipped is True:
            out.add(r["code"])
    return out


def s_boll_squeeze(rows, **kw):
    """布林带宽挤压：当前带宽处于近 120 日最低分位（默认 ≤10%）。

    蒸馏源：volatility/bollinger_band_width.go（配套 percent_b.go）。

    ## 为什么在 A 股做这个策略

    本项目实测（market_breadth 的等权指数口径）：492 个有效交易日里只有
    **10 天**是趋势日（全挤在 2025-04-07~09），近 120 天为 **0**——A 股长期
    震荡，趋势指标大面积失效。震荡市里少数有信息量的信号就是「波动收缩到
    极致」：挤压本身不指方向，但往往先于变盘，所以本策略挑的是
    「弹簧已压紧」的票，**配合放量突破确认使用**，不要单独当买点。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    pct_th = _p(kw, "squeeze_pct", 10.0)
    for r in rows:
        s = _series(hist, r["code"])
        if not s:
            continue
        c = s["close"]
        # 历史太短的票要跳过：只有 30 根时带宽序列仅 11 个值（粒度 9%），
        # 「近 120 日分位」其实是在极短窗口里排的，会系统性偏向次新股。
        # 宁可漏掉，也不要这种看着像信号、实为数据不足的偏差。
        if len(c) < 80:
            continue
        sq = bollinger_squeeze_last(c, 20, 2.0, 120)
        if sq and sq["pct"] <= pct_th:
            out.add(r["code"])
    return out


def s_atr_breakout(rows, **kw):
    """ATR 通道突破：收盘越过「昨收 + 2×ATR(14)」的波动率自适应突破。

    蒸馏源：volatility/atr.go。突破阈值随近期波动放大/收缩，比固定百分比
    突破更不易在震荡市频繁假突破。需至少 15 根历史算 ATR(14)。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    mult = _p(kw, "break_mult", 2.0)
    for r in rows:
        s = _series(hist, r["code"])
        if not s:
            continue
        atr = atr_last(s["high"], s["low"], s["close"], 14)
        if atr is None:
            continue
        c = float(s["close"][-1]); pc = float(s["close"][-2])
        if pc <= 0 or c <= 0:
            continue
        # 波动率自适应突破：收盘显著越过「昨收 + mult×ATR」
        if c > pc + mult * atr:
            out.add(r["code"])
    return out


def s_connors_rsi_dip(rows, **kw):
    """康纳丝超卖：ConnorsRSI(100) < 15。

    蒸馏源：momentum/connors_rsi.go。ConnorsRSI = mean(RSI(3),
    RSI(2 of 连跌streak), PercentRank(100 of ROC1))，把「短期超卖 +
    连跌惯性 + 相对自身近期跌幅」三件事揉成一个 0~100 的值。<15 为深度
    超卖（源码默认阈值）。需至少 102 根历史（lookback=100）。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    th = _p(kw, "crsi_th", 15.0)
    for r in rows:
        s = _series(hist, r["code"])
        if not s:
            continue
        crsi = connors_rsi_last(s["close"], lookback=100)
        if crsi is None:
            continue
        if crsi < th:
            out.add(r["code"])
    return out


def s_td9_buy(rows, **kw):
    """TD 九转抄底：连续 9 根收盘 < 4 日前收盘（TD setup 计满 9）。

    蒸馏源：td_sequential.go。逐日统计「当日收盘 < 4 日前收盘」的连续天数，
    计满 9 即经典「TD9 买入 setup 完成」（统计学的极端超卖点）。只取恰好
    计满 9 的那一根（count==9），避免计数持续累积后天天误报。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    for r in rows:
        s = _series(hist, r["code"])
        if not s:
            continue
        cnt = td_count_last(s["close"])
        if cnt == 9:
            out.add(r["code"])
    return out


def s_mfi_oversold(rows, **kw):
    """资金流超卖：MFI(14) < 20。

    蒸馏源：volume/mfi.go。MFI = 量权 RSI —— 用典型价×成交量代替 RSI 的
    收盘价涨跌，把「资金流向」纳入超卖判定。带量下跌使 MFI 走低，<20 为
    超卖。需至少 15 根历史。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    th = _p(kw, "mfi_th", 20.0)
    for r in rows:
        s = _series(hist, r["code"])
        if not s:
            continue
        mfi = mfi_last(s["high"], s["low"], s["close"], s["volume"], 14)
        if mfi is None:
            continue
        if mfi < th:
            out.add(r["code"])
    return out


def s_cmf_breakout(rows, **kw):
    """资金流入突破：CMF(20) > 0.1 且收盘创 20 日新高。

    蒸馏源：volume/cmf.go。CMF = Σ(CLV×Vol,20)/Σ(Vol,20)，衡量近 20 日
    资金是净流入(>0)还是净流出(<0)。要求 CMF 明显为正（>0.1）的同时价格
    创 20 日新高 —— 量价齐升的启动点。需至少 20 根历史。
    """
    hist = _hist_of(kw)
    out = set()
    if hist is None:
        return out
    th = _p(kw, "cmf_th", 0.1)
    for r in rows:
        s = _series(hist, r["code"])
        if not s:
            continue
        cmf = cmf_last(s["high"], s["low"], s["close"], s["volume"], 20)
        if cmf is None or cmf <= th:
            continue
        c = s["close"]
        if len(c) < 20:
            continue
        # 收盘创 20 日新高（含当日）。带极小容差吸收 float32 误差，
        # 平前高不算新高（见 history.EPS 同理）。
        cur = float(c[-1]); prev_max = float(np.max(c[-20:-1]))
        if cur < prev_max * (1.0 - 1e-5):
            continue
        out.add(r["code"])
    return out


# ===========================================================================
# 策略注册表
# ===========================================================================

STRATEGY_DEFS = [
    # 趋势 / 形态
    {"key": "breakout_20h",  "name": "突破新高",     "cat": "趋势形态", "fn": s_breakout_20h,
     "desc": "当日最高创 20 日新高，收盘站稳突破位", "hist": True},
    {"key": "ma_bull",       "name": "均线多头",     "cat": "趋势形态", "fn": s_ma_bull,
     "desc": "MA5>MA10>MA20>MA60 多头排列，价在 MA5 上", "hist": True},
    {"key": "platform_break","name": "平台突破",     "cat": "趋势形态", "fn": s_platform_break,
     "desc": "前60日振幅≤35%，放量突破平台上沿", "hist": True},
    {"key": "gap_up",        "name": "跳空强势",     "cat": "趋势形态", "fn": s_gap_up,
     "desc": "高开 2%+ 且收盘不破开盘"},

    # 量价 / 涨停
    {"key": "limit_up",       "name": "涨停封板",    "cat": "量价涨停", "fn": s_limit_up,
     "desc": "当日封涨停（按板块判定 10/20/30%）"},
    {"key": "near_limit",     "name": "接近涨停",    "cat": "量价涨停", "fn": s_near_limit,
     "desc": "涨幅达涨停幅度的 70%~99%"},
    {"key": "limit_momentum", "name": "涨停动量",    "cat": "量价涨停", "fn": s_limit_up_momentum,
     "desc": "涨停 或 大涨 7%+ 且换手 5%+"},
    {"key": "strong_turnover","name": "高换手强势",  "cat": "量价涨停", "fn": s_strong_turnover,
     "desc": "换手 8%+ 且上涨 2%+"},
    {"key": "volume_surge",   "name": "放量突破",    "cat": "量价涨停", "fn": s_volume_surge,
     "desc": "量比≥2倍（对比前20日均量）且涨幅 3%+", "hist": True},
    {"key": "vol_price_up",   "name": "量价齐升",    "cat": "量价涨停", "fn": s_vol_price_up,
     "desc": "涨幅 3%+ 且换手 3%+"},

    # 反转 / 波动
    {"key": "oversold_rebound","name": "超跌反弹",   "cat": "反转波动", "fn": s_oversold_rebound,
     "desc": "前20日跌≥15%，当日企稳反弹 1%~6%", "hist": True},
    {"key": "reversal_hammer", "name": "长下影反击", "cat": "反转波动", "fn": s_reversal_hammer,
     "desc": "长下影 2 倍实体，收盘转涨"},
    {"key": "pullback_ma20",   "name": "回踩企稳",   "cat": "反转波动", "fn": s_pullback_ma20,
     "desc": "MA20>MA60 趋势中，缩量(≤0.85)回踩 MA20±2%", "hist": True},
    {"key": "low_vol_leader",  "name": "低波动龙头", "cat": "反转波动", "fn": s_low_vol_leader,
     "desc": "振幅≤2.5%、小幅上涨、中大市值"},
    {"key": "small_cap_active","name": "小市值活跃", "cat": "反转波动", "fn": s_small_cap_active,
     "desc": "市值<100亿、换手 5%+、上涨"},

    # 形态评分（借鉴 a-share-quant-selector 的 B1 思路，按本项目实测重设计）
    # 与上面 15 个「是/否」型策略不同，这是**打分型**策略：按 4 个维度的
    # 实测有利方向加权评分（低位 + 缩量 + 前期滞涨），取高分。
    {"key": "pattern_score",  "name": "形态评分",   "cat": "形态相似度",
     "fn": s_pattern_score, "desc": "低位+缩量+前期滞涨（实测有利形态，≥85分）",
     "hist": True},

    # ------------------------------------------------------------------
    # 经典指标（方案 A · 蒸馏自 cinar/indicator）
    # 全部只用 OHLCV+amount（daily_bars 可直接重建），故全部进体检白名单。
    {"key": "supertrend_long", "name": "超级趋势翻多", "cat": "经典指标",
     "fn": s_supertrend_long,
     "desc": "SuperTrend(10, 3×ATR) 由空翻多的当日", "hist": True},
    {"key": "atr_breakout",  "name": "ATR突破",       "cat": "经典指标",
     "fn": s_atr_breakout,
     "desc": "收盘越过 昨收+2×ATR(14) 的波动率自适应突破", "hist": True},
    {"key": "connors_rsi_dip","name": "康纳丝超卖",    "cat": "经典指标",
     "fn": s_connors_rsi_dip, "desc": "ConnorsRSI(100) < 15 的深度超卖",
     "hist": True},
    {"key": "td9_buy",       "name": "TD九转抄底",     "cat": "经典指标",
     "fn": s_td9_buy,
     "desc": "连续 9 根收盘<4日前收盘（TD setup 计满 9）", "hist": True},
    {"key": "mfi_oversold",  "name": "资金流超卖",      "cat": "经典指标",
     "fn": s_mfi_oversold, "desc": "MFI(14) < 20 的带量超卖", "hist": True},
    {"key": "cmf_breakout",  "name": "资金流入突破",    "cat": "经典指标",
     "fn": s_cmf_breakout,
     "desc": "CMF(20)>0.1 且收盘创 20 日新高（量价齐升启动）", "hist": True},
    {"key": "boll_squeeze",  "name": "布林挤压",       "cat": "经典指标",
     "fn": s_boll_squeeze,
     "desc": "带宽处于近120日最低10%分位——弹簧压紧，往往先于变盘（不指方向，配放量突破用）",
     "hist": True},

    # ------------------------------------------------------------------
    # 分钟级策略（"intraday": True）
    #
    # 与上面所有策略的本质区别：**数据源不同**。
    # 上面走日线（history 引擎，落库 + 全市场内存缓存），
    # 这几个走实时分钟线（intraday 引擎，network IO + 15 秒缓存）。
    #
    # 因此它们【不能】用 run_strategies 跑——那里传进来的是行情快照 rows，
    # 而分钟线需要另一次全市场拉取（约 18 秒）。入口在
    # app.py 的 /api/intraday_scan，判定逻辑见 INTRADAY_PRESETS。
    #
    # 这里仍然登记它们，是为了：
    #   1. 让 /api/strategy_list 能完整展示策略目录（含分钟级）；
    #   2. 前端能据此把「日线策略」与「分钟策略」分区渲染；
    #   3. 走查回测等日线工具能通过 intraday 标记跳过它们。
    # fn 设为 None，run_strategies 会跳过（见 run_strategies 的过滤）。
    {"key": "id_vol_surge",   "name": "盘中放量",   "cat": "分钟级",
     "fn": None, "intraday": True,
     "desc": "分钟线最后一根量 ≥ 前20根均量的 3 倍（盘中资金异动）"},
    {"key": "id_break_up",    "name": "分时突破",   "cat": "分钟级",
     "fn": None, "intraday": True,
     "desc": "现价突破前 20 根分钟 K 线的最高价"},
    {"key": "id_up_strong",   "name": "盘中强势",   "cat": "分钟级",
     "fn": None, "intraday": True,
     "desc": "当日涨 >2% 且位于当日区间上半部"},
    {"key": "id_pullback_day","name": "日内回踩",   "cat": "分钟级",
     "fn": None, "intraday": True,
     "desc": "当日下跌但现价已从当日低点明显回升"},
    {"key": "id_vol_price_up","name": "分时价涨量增","cat": "分钟级",
     "fn": None, "intraday": True,
     "desc": "最后一根分钟 K 线价涨且量比 ≥1.5"},
    {"key": "id_range_break", "name": "横盘异动",   "cat": "分钟级",
     "fn": None, "intraday": True,
     "desc": "前20根振幅 <2% 但最后一根突然放量 ≥3 倍"},
]

STRATEGY_BY_KEY = {d["key"]: d for d in STRATEGY_DEFS}

# 把可调参数挂到策略定义上（单一数据源：PARAM_DEFAULTS）。
# 这样 list_strategies()、走查回测、前端都能拿到「本策略有哪些参数、默认多少」。
for _d in STRATEGY_DEFS:
    _p_defaults = PARAM_DEFAULTS.get(_d["key"]) or {}
    _d["params"] = dict(_p_defaults)
    _d["tunable"] = bool(_p_defaults)


def list_strategies():
    """返回给前端的策略目录（不含函数）

    带 "hist": True 的策略需要历史日线，前端可据此提示用户
    （例如在数据未就绪时置灰或加标记）。

    "params" 是该策略的可调阈值及其默认值（见 PARAM_DEFAULTS），
    "tunable" 表示是否可调（= params 非空）。走查回测会用它生成参数网格。
    "intraday" 表示这是分钟级策略，数据源与执行入口都不同——
    前端据此把它单独分区，且不要交给 /api/strategy_scan。
    """
    cats = {}
    for d in STRATEGY_DEFS:
        cats.setdefault(d["cat"], []).append({
            "key": d["key"], "name": d["name"], "desc": d["desc"],
            "needs_history": bool(d.get("hist")),
            "intraday": bool(d.get("intraday")),
            "params": dict(d.get("params") or {}),
            "tunable": bool(d.get("tunable")),
        })
    return [{"cat": k, "items": v} for k, v in cats.items()]


def strategy_meta(key: str):
    """取单个策略的定义（含是否依赖历史数据）。"""
    return STRATEGY_BY_KEY.get(key)


def get_history_engine(auto_load: bool = True):
    """取历史指标引擎。策略通过它拿到真实的均线/量能/新高数据。

    单独包一层是为了：① 便于测试时注入假引擎；② 失败时可优雅降级。
    """
    try:
        from history import get_engine
        return get_engine(auto_load=auto_load)
    except Exception:
        return None


def run_strategy(rows, key, **params):
    """在快照上运行单个策略，返回命中行（保持原行序）。

    若策略声明需要历史数据而当前无引擎，会返回空集（而非退回伪实现）。
    """
    d = STRATEGY_BY_KEY.get(key)
    if not d:
        return []
    if d.get("hist") and "hist" not in params:
        eng = get_history_engine()
        if eng is not None:
            params = dict(params, hist=eng)
    hit = d["fn"](rows, **params)
    return [r for r in rows if r["code"] in hit]


def _data_lag(hist_as_of: str, rows, hist=None) -> tuple:
    """比较「历史日线最新交易日」与「快照所属交易日」，返回 (滞后天数, 提示语)。

    快照本身不带交易日字段，但它的 `prev_close`（昨收）就是「上一交易日
    的收盘价」。用样本股比对「历史末根收盘」与「快照昨收」：

        相等 → 历史停在「快照的前一天」，即历史 = 昨日，滞后 ≥ 1 个交易日
        不等 → 历史已包含快照当日（盘后场景），滞后 = 0

    实现要点：用**中位数式的多数票**判断，而不是逐只精确匹配——
    个别除权股（XD）的 prev_close 会被调整，不能用它代表整体。

    返回 (None, "") 表示无法判断（不猜）。
    """
    if not hist_as_of:
        return None, ""
    try:
        import datetime as _dt
        d_hist = _dt.date.fromisoformat(str(hist_as_of)[:10])
    except Exception:
        return None, ""

    # 判据 A：历史末根收盘 vs 快照昨收的命中率
    #   历史停在昨日 → 大量相等；历史已含当日 → 大量不等
    matched = 0
    total = 0
    if hist is not None:
        for r in (rows or [])[:400]:
            prev = r.get("prev_close") or 0
            if prev <= 0:
                continue
            s = hist._data.get(r.get("code"))
            if not s:
                continue
            total += 1
            try:
                # 历史序列以「股」计价，快照 prev_close 也是；直接比
                if abs(float(s["close"][-1]) - prev) / prev < 0.001:
                    matched += 1
            except Exception:
                pass
            if total >= 200:
                break

    if total >= 20:
        aligned_to_yesterday = matched / total > 0.8   # 多数相等 → 历史=昨日
    else:
        # 样本不足时退回「自然日差」推断：历史交易日 < 今天 就认为滞后
        aligned_to_yesterday = d_hist < _dt.date.today()

    if not aligned_to_yesterday:
        return 0, ""                      # 历史已含当日（盘后），完全对齐

    # 折算成交易日数（跳过周末，用于措辞）
    today = _dt.date.today()
    gap = 0
    cur = d_hist
    while cur < today:
        cur += _dt.timedelta(days=1)
        if cur.weekday() < 5:
            gap += 1
    if gap <= 0:
        gap = 1

    note = (f"指标基于 {hist_as_of} 收盘，"
            f"{today.isoformat()} 当日涨跌未纳入，盘中结果仅供参考")
    return gap, note


def _apply_risk_filters(out, tags, diag, hist=None,
                        risk_filters=True, mvd_lookback=20,
                        j_anomaly_lookback=30, j_anomaly_thresh=80.0, **_):
    """全局风险过滤（借鉴 a-share-quant-selector 碗口反弹的两道廉价防御）。

    1) 最大阴量过滤（max_vol_down）：回溯 mvd_lookback 根 K 线，成交量最大的
       那根若为阴线（收盘 < 开盘）→ 整只剔除。对「放量出货」的廉价防御：
       回溯期内放出的最大量是在砸盘而非吸筹，其余信号再好也先回避。

    2) J 值异常过滤（j_anomaly）：近 j_anomaly_lookback 根的 |J| 均值 > 阈值
       → 剔除。J 长期钉在极端值通常是复权缺口/数据错位导致 KDJ 失真，
       属数据卫生检查，成本≈0。

    约定：
    - 只对历史引擎里有数据的票生效；无历史/数据不足一律放行（不猜）；
    - risk_filters=False 整体关闭；lookback/threshold 可经 tune 覆盖；
    - **_ 吸收策略自身的无关参数（tune 透传），避免 TypeError；
    - 剔除明细进 diag["risk_filter"]（removed 截前 50 条防膨胀）。
    """
    diag["risk_filter"] = {"enabled": bool(risk_filters), "max_vol_down": 0,
                           "j_anomaly": 0, "removed": []}
    if not risk_filters or not out or hist is None:
        return out, tags, diag

    kept, removed = [], []
    for r in out:
        code = r.get("code")
        s = hist._data.get(code)
        cl = (s or {}).get("close")
        if not s or cl is None or len(cl) < 10:
            kept.append(r)
            continue
        drop = ""
        # ---- 1) 最大阴量 ----
        m = min(int(mvd_lookback), len(s["close"]))
        if m >= 5:
            try:
                vo = np.asarray(s["volume"][-m:], dtype=float)
                im = int(np.argmax(vo))
                c, o = float(s["close"][-m + im]), float(s["open"][-m + im])
                if c < o:
                    drop = f"{m}日内最大成交量为阴线"
            except Exception:
                pass
        # ---- 2) J 值异常 ----
        if not drop and len(s["close"]) >= int(j_anomaly_lookback) + 9:
            try:
                _, _, J = KDJ(np.asarray(s["close"], dtype=float),
                              np.asarray(s["high"], dtype=float),
                              np.asarray(s["low"], dtype=float))
                jn = J[-int(j_anomaly_lookback):]
                jm = float(np.nanmean(jn)) if len(jn) else 0.0
                if abs(jm) > float(j_anomaly_thresh):
                    drop = f"|J|均值{abs(jm):.0f}>{int(j_anomaly_thresh)}（KDJ数据失真）"
            except Exception:
                pass

        if drop:
            removed.append({"code": code, "name": r.get("name", ""), "reason": drop})
            tags.pop(code, None)
        else:
            kept.append(r)

    diag["risk_filter"]["max_vol_down"] = sum(1 for x in removed if "阴线" in x["reason"])
    diag["risk_filter"]["j_anomaly"] = sum(1 for x in removed if "J值" in x["reason"] or "|J|" in x["reason"])
    diag["risk_filter"]["removed"] = removed[:50]
    return kept, tags, diag


def run_strategies(rows, keys, mode="union", **params):
    """运行多个策略：union(并集) / intersect(交集)。

    返回 (rows, 命中说明, 诊断)。

    诊断字段（第三个返回值）说明本次扫描中历史数据的使用情况：
        {"hist_ready": bool, "hist_as_of": str, "need_hist": [...],
         "skipped": [...], "data_lag_days": int|null, "data_note": str}
    便于前端/调用方知道「结果为空」是因为没选出股票，还是因为
    历史数据没就绪。这两者的区别对使用者很重要。

    ## data_lag_days 是什么（重要）

    历史日线的最后一根是**某个交易日收盘**，而行情快照可能是**当天盘中**。
    两者相差几个自然日就记为几，0 表示完全对齐（通常发生在盘后）。

        data_lag_days == 0  历史与快照同日 → 指标完全准确
        data_lag_days >= 1  指标基于更早的交易日 → 当日涨跌未被纳入

    这不是 bug，而是「盘中运行时历史数据天然滞后」的客观事实。
    透出这个值是为了让前端能明确提示用户，避免把盘中结果当成盘后结果。
    """
    if not keys:
        return [], {}, {}

    # ---- 按需注入历史引擎 ----
    need_hist = [k for k in keys
                 if (STRATEGY_BY_KEY.get(k) or {}).get("hist")]
    diag = {"hist_ready": False, "hist_as_of": "", "need_hist": need_hist,
            "skipped": [], "data_lag_days": None, "data_note": ""}

    if need_hist:
        # 引擎可能来自调用方（params["hist"]），也可能需要我们自己取。
        # 无论哪种来源，诊断信息都要如实反映它的状态与数据新鲜度。
        eng = params.get("hist")
        if eng is None:
            eng = get_history_engine()
            if eng is None:
                diag["skipped"] = list(need_hist)
            else:
                params = dict(params, hist=eng)
        if eng is not None:
            try:
                st = eng.stats() or {}
                diag["hist_ready"] = bool(st.get("codes"))
                diag["hist_as_of"] = st.get("as_of", "")
                # 历史交易日 vs 快照交易日：算出滞后天数，供前端提示
                lag, note = _data_lag(diag["hist_as_of"], rows, hist=eng)
                diag["data_lag_days"] = lag
                diag["data_note"] = note
            except Exception:
                pass

    hit_sets = {}
    skipped_no_fn = []
    for k in keys:
        d = STRATEGY_BY_KEY.get(k)
        if not d:
            continue
        # 分钟级策略的 fn 是 None：它们的数据源是实时分钟线，
        # 不能用这里的行情快照 rows 跑（见 STRATEGY_DEFS 里的注释）。
        # 必须显式跳过，否则会因调用 None 而抛 TypeError。
        if d.get("fn") is None:
            skipped_no_fn.append(k)
            continue
        hit_sets[k] = d["fn"](rows, **params)
    diag["skipped_no_fn"] = skipped_no_fn
    diag["intraday_hint"] = (
        "以下策略需走分钟线入口 /api/intraday_scan，本接口不处理："
        + ", ".join(skipped_no_fn)) if skipped_no_fn else ""
    if not hit_sets:
        return [], {}, diag

    if mode == "intersect" and len(hit_sets) > 1:
        codes = set.intersection(*hit_sets.values())
    else:
        codes = set.union(*hit_sets.values())

    out = [r for r in rows if r["code"] in codes]
    # 每个命中股票标出命中了哪些策略
    tags = {}
    for r in out:
        tags[r["code"]] = [k for k, s in hit_sets.items() if r["code"] in s]

    # 全局风险过滤（借鉴 qs 最大阴量 + J 值异常）：合成结果后统一剔除
    p2 = dict(params)
    eng = p2.pop("hist", None) or get_history_engine()
    out, tags, diag = _apply_risk_filters(out, tags, diag, hist=eng, **p2)

    return out, tags, diag
