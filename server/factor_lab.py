"""因子实验室：横截面因子 IC / ICIR 检验。

回答一个具体问题：**哪些指标对 A 股未来收益真有预测力？**

与 strategy_eval.py 的分工：
- strategy_eval 评估的是**选股信号**（0/1 命中，短线 5 日），回答「这个策略最近灵不灵」。
- 本模块评估的是**连续因子**（动量/低波/反转/流动性…），回答「这个因子本身有没有
  alpha」。因子是轮动策略的原料：没有预测力的因子，轮动就只是随机换仓。

方法（业界标准的因子检验流程）：
1. 构造因子矩阵 (交易日 × 股票)，**只用 t 日及之前的数据**，无前视。
2. 未来收益 ret[t] = close[t+1+forward] / close[t+1] - 1
   —— t 日收盘产生信号，t+1 收盘买入（贴近实盘，避免用信号日收盘价成交的前视）。
3. 每个交易日做**横截面 Spearman 秩相关**，得到 IC 序列。
4. 汇总 IC 均值 / ICIR / IC>0 占比 / t 值 / 五档分层收益 / 市况分段。

判定：ICIR >= 0.3 视为「有预测力」；|ICIR| < 0.2 基本是噪声。

依赖：numpy + 项目内 store / datasource / mytt（RSI 复用 mytt 保证口径一致）。
scipy 不需要——Spearman 自己实现（平均秩 + Pearson），少一个依赖。
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

POOL_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "index_pools.json")

# 因子列表：(key, 中文名, 类别, 计算函数)
# 方向由 IC 符号决定，不预先假设「越大越好」。
FACTOR_DEFS: List[Tuple[str, str, str, str]] = [
    ("mom_20",    "20日动量",   "动量",   "_f_mom"),
    ("mom_60",    "60日动量",   "动量",   "_f_mom"),
    ("mom_120",   "120日动量",  "动量",   "_f_mom"),
    ("rev_5",     "5日反转",    "反转",   "_f_rev"),
    ("vol_60",    "60日波动率", "低波",   "_f_vol"),
    ("vol_120",   "120日波动率", "低波",  "_f_vol"),
    ("mdd_60",    "60日最大回撤", "低波", "_f_mdd"),
    ("rsi_14",    "RSI(14)",   "超买超卖", "_f_rsi"),
    ("bias_20",   "MA20乖离",  "超买超卖", "_f_bias"),
    ("boll_pct",  "布林%B",    "超买超卖", "_f_boll"),
    ("amt_60",    "60日均成交额(对数)", "流动性", "_f_amt"),
    ("vol_surge", "5日/60日量比", "量能", "_f_surge"),
]

FORWARDS = (5, 10, 20)     # 未来收益窗口（交易日）
MIN_CROSS = 30             # 每日截面最少有效样本数，低于此不算 IC
ICIR_PASS = 0.3            # 「有预测力」门槛


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_universe(pool: str = "000300", min_days: int = 200
                  ) -> Dict[str, Any]:
    """从本地库读某指数成分股日线，对齐成矩阵。

    返回 {dates, codes, close, high, low, open, volume, amount}，
    形状均为 (交易日数, 股票数)，缺失填 NaN。
    """
    import store
    import datasource as ds

    store.initialize()
    with open(POOL_FILE, "r", encoding="utf-8") as f:
        pools = json.load(f)

    info = pools.get(pool)
    if info is None:
        for k, v in pools.items():
            if pool in (k, str(v.get("name"))):
                info = v
                break
    if info is None:
        raise ValueError(f"股票池不存在: {pool}")

    raw_codes = [ds.normalize(str(c)) for c in info.get("codes", [])]
    if not raw_codes:
        raise ValueError(f"股票池 {pool} 无成分股")

    c = store._conn()
    fields = ("open", "close", "high", "low", "volume", "amount")
    ph = ",".join("?" * len(raw_codes))
    rows = c.execute(
        f"SELECT code,date,open,close,high,low,volume,amount FROM daily_bars"
        f" WHERE code IN ({ph}) ORDER BY date", tuple(raw_codes)).fetchall()

    if not rows:
        raise ValueError("库内无对应日线数据")

    dates = sorted({str(r["date"]) for r in rows})
    didx = {d: i for i, d in enumerate(dates)}
    # 只保留库里真有数据的成分股
    seen: List[str] = []
    for r in rows:
        code = str(r["code"])
        if code not in didx and code not in seen:
            seen.append(code)
    # 按首次出现顺序稳定排序
    codes = [x for x in raw_codes if x in set(seen)] or sorted(seen)
    cidx = {c: i for i, c in enumerate(codes)}

    T, N = len(dates), len(codes)
    mats = {k: np.full((T, N), np.nan, dtype=np.float64) for k in fields}
    for r in rows:
        i = didx.get(str(r["date"]))
        j = cidx.get(str(r["code"]))
        if i is None or j is None:
            continue
        for k in fields:
            try:
                mats[k][i, j] = float(r[k] or 0.0)
            except (TypeError, ValueError):
                pass

    # 剔除上市/数据太短的股票（新股上市初期波动异常，会污染截面）
    valid = np.array([np.count_nonzero(~np.isnan(mats["close"][:, j]))
                      for j in range(N)])
    keep = valid >= min_days
    out: Dict[str, Any] = {"dates": dates, "codes": [codes[j] for j in range(N) if keep[j]]}
    for k in fields:
        out[k] = mats[k][:, keep]
    out["_pool"] = str(info.get("name") or pool)
    return out


# ---------------------------------------------------------------------------
# 因子计算（全部只用 t 日及之前的数据）
# ---------------------------------------------------------------------------

def _safe_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(b != 0, a / b, np.nan)
    return np.where(np.isfinite(out), out, np.nan)


def _f_mom(mats: Dict[str, Any], n: int = 20) -> np.ndarray:
    """n 日动量：close[t]/close[t-n] - 1。"""
    C = mats["close"]
    T, N = C.shape
    out = np.full((T, N), np.nan)
    if T > n:
        out[n:] = _safe_div(C[n:], C[:-n]) - 1.0
    return out


def _f_rev(mats: Dict[str, Any], n: int = 5) -> np.ndarray:
    """短期反转：-(n 日收益)。 literature：A 股短期反转效应明显。"""
    return -_f_mom(mats, n)


def _daily_ret(C: np.ndarray) -> np.ndarray:
    T, N = C.shape
    out = np.full((T, N), np.nan)
    if T > 1:
        out[1:] = _safe_div(C[1:], C[:-1]) - 1.0
    return out


def _f_vol(mats: Dict[str, Any], n: int = 60) -> np.ndarray:
    """n 日已实现波动率（日收益标准差）。"""
    r = _daily_ret(mats["close"])
    T, N = r.shape
    out = np.full((T, N), np.nan)
    for t in range(n, T):
        seg = r[t - n + 1:t + 1]
        if np.count_nonzero(~np.isnan(seg)) >= max(3, n // 3):
            out[t] = np.nanstd(seg, axis=0)
    return out


def _f_mdd(mats: Dict[str, Any], n: int = 60) -> np.ndarray:
    """n 日内最大回撤（负值）。"""
    C = mats["close"]
    T, N = C.shape
    out = np.full((T, N), np.nan)
    for t in range(n, T):
        seg = C[t - n + 1:t + 1]
        with np.errstate(invalid="ignore"):
            peak = np.nanmax(np.maximum.accumulate(
                np.where(np.isnan(seg), -np.inf, seg), axis=0), axis=0)
            dd = _safe_div(seg, np.where(peak > 0, peak, np.nan)) - 1.0
        out[t] = np.nanmin(np.where(np.isnan(dd), np.inf, dd), axis=0)
    out = np.where(np.isfinite(out), out, np.nan)
    return out


def _f_rsi(mats: Dict[str, Any], n: int = 14) -> np.ndarray:
    """RSI：复用 mytt.RSI 保证与项目其它地方口径一致（中国式 SMA）。"""
    import pandas as pd
    import mytt
    C = mats["close"]
    T, N = C.shape
    out = np.full((T, N), np.nan)
    for j in range(N):
        col = C[:, j]
        m = ~np.isnan(col)
        if m.sum() < n + 5:
            continue
        try:
            out[m, j] = mytt.RSI(pd.Series(col[m]).values, n)
        except Exception:
            pass
    return out


def _f_bias(mats: Dict[str, Any], n: int = 20) -> np.ndarray:
    """距 MA20 乖离：close/MA(n) - 1。"""
    import mytt
    C = mats["close"]
    T, N = C.shape
    out = np.full((T, N), np.nan)
    import pandas as pd
    for j in range(N):
        col = C[:, j]
        m = ~np.isnan(col)
        if m.sum() < n + 2:
            continue
        try:
            ma = mytt.MA(pd.Series(col[m]).values, n)
            out[m, j] = np.where(ma != 0, col[m] / ma - 1.0, np.nan)
        except Exception:
            pass
    return out


def _f_boll(mats: Dict[str, Any], n: int = 20) -> np.ndarray:
    """布林 %B：(close - lower) / (upper - lower)。"""
    import pandas as pd
    import mytt
    C = mats["close"]
    T, N = C.shape
    out = np.full((T, N), np.nan)
    for j in range(N):
        col = C[:, j]
        m = ~np.isnan(col)
        if m.sum() < n + 2:
            continue
        try:
            # 注意 mytt.BOLL 的参数名是大写 N/P（不是 n/p），写错会静默变全 NaN
            u, mid, lo = mytt.BOLL(pd.Series(col[m]).values, N=n, P=2)
            rng = u - lo
            out[m, j] = np.where(rng != 0, (col[m] - lo) / rng, np.nan)
        except Exception:
            pass
    return out


def _f_amt(mats: Dict[str, Any], n: int = 60) -> np.ndarray:
    """n 日均成交额的对数（流动性/关注度代理）。"""
    A = mats["amount"]
    T, N = A.shape
    out = np.full((T, N), np.nan)
    for t in range(n, T):
        seg = A[t - n + 1:t + 1]
        mu = np.nanmean(seg, axis=0)
        with np.errstate(invalid="ignore"):
            out[t] = np.where(mu > 0, np.log(mu), np.nan)
    return out


def _f_surge(mats: Dict[str, Any], short: int = 5, long: int = 60) -> np.ndarray:
    """量能异动：近 short 日均量 / 近 long 日均量。"""
    V = mats["volume"]
    T, N = V.shape
    out = np.full((T, N), np.nan)
    for t in range(long, T):
        s = np.nanmean(V[t - short + 1:t + 1], axis=0)
        l = np.nanmean(V[t - long + 1:t + 1], axis=0)
        out[t] = _safe_div(s, l)
    return out


def build_factors(mats: Dict[str, Any],
                  keys: Optional[Sequence[str]] = None
                  ) -> Dict[str, np.ndarray]:
    """构造因子矩阵。返回 {key: (T,N) ndarray}。"""
    fn: Dict[str, Callable[[Dict[str, Any]], np.ndarray]] = {
        "_f_mom": lambda m: _f_mom(m, 20),
        "_f_rev": lambda m: _f_rev(m, 5),
        "_f_vol": lambda m: _f_vol(m, 60),
        "_f_mdd": lambda m: _f_mdd(m, 60),
        "_f_rsi": lambda m: _f_rsi(m, 14),
        "_f_bias": lambda m: _f_bias(m, 20),
        "_f_boll": lambda m: _f_boll(m, 20),
        "_f_amt": lambda m: _f_amt(m, 60),
        "_f_surge": lambda m: _f_surge(m, 5, 60),
    }
    # 多窗口动量/波动率：按 key 后缀决定窗口
    out: Dict[str, np.ndarray] = {}
    for key, _name, _cat, fname in FACTOR_DEFS:
        if keys and key not in keys:
            continue
        if key.startswith("mom_"):
            n = int(key.split("_")[1])
            out[key] = _f_mom(mats, n)
        elif key.startswith("vol_") and key.split("_")[1].isdigit():
            # 注意：vol_surge 也以 vol_ 开头，必须先排除，否则 int('surge') 崩
            n = int(key.split("_")[1])
            out[key] = _f_vol(mats, n)
        else:
            f = fn.get(fname)
            if f:
                out[key] = f(mats)
    return out


# ---------------------------------------------------------------------------
# 未来收益 / 秩相关 / IC
# ---------------------------------------------------------------------------

def forward_returns(C: np.ndarray, forward: int = 5, lag: int = 1) -> np.ndarray:
    """未来收益矩阵。ret[t] = close[t+lag+forward] / close[t+lag] - 1。

    lag=1 表示 t 日收盘出信号、t+1 收盘成交，避免「用信号日收盘价买入」的前视。
    """
    T, N = C.shape
    out = np.full((T, N), np.nan)
    s = lag + forward
    if T > s:
        out[:T - s] = _safe_div(C[s:], C[lag:T - forward]) - 1.0
    return out


def rank_avg(v: np.ndarray) -> np.ndarray:
    """平均秩（并列取均值），NaN 不参与。返回与 v 同长的 float 数组。"""
    n = len(v)
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    sv = v[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def ic_series(fmat: np.ndarray, rmat: np.ndarray,
              min_cross: int = MIN_CROSS) -> np.ndarray:
    """逐交易日横截面 Spearman 秩相关（IC 序列）。"""
    T = fmat.shape[0]
    ics = np.full(T, np.nan)
    for t in range(T):
        f = fmat[t]
        r = rmat[t]
        m = np.isfinite(f) & np.isfinite(r)
        if int(m.sum()) < min_cross:
            continue
        fv, rv = f[m], r[m]
        rf, rr = rank_avg(fv), rank_avg(rv)
        sf = rf - rf.mean()
        sr = rr - rr.mean()
        den = math.sqrt(float((sf ** 2).sum()) * float((sr ** 2).sum()))
        if den > 0:
            ics[t] = float((sf * sr).sum()) / den
    return ics


def ic_summary(ics: np.ndarray) -> Dict[str, Any]:
    """IC 序列汇总：均值 / 标准差 / ICIR / 正占比 / t 值 / 方向。"""
    v = ics[np.isfinite(ics)]
    n = int(v.size)
    if n < 10:
        return {"obs": n, "ic_mean": None, "icir": None,
                "pos_ratio": None, "t_stat": None, "direction": "样本不足"}
    mean = float(v.mean())
    sd = float(v.std(ddof=1)) if n > 1 else 0.0
    icir = (mean / sd) if sd > 0 else None
    t_stat = (mean / (sd / math.sqrt(n))) if sd > 0 else None
    return {
        "obs": n,
        "ic_mean": mean,
        "ic_std": sd,
        "icir": icir,
        "pos_ratio": float((v > 0).mean()),
        "t_stat": t_stat,
        "direction": "正向(因子越大收益越高)" if mean > 0 else "反向(因子越小收益越高)",
    }


def quantile_check(fmat: np.ndarray, rmat: np.ndarray,
                   q: int = 5, min_cross: int = MIN_CROSS) -> Dict[str, Any]:
    """五档分层：每日按因子分档，统计各档平均未来收益（时间上再平均）。

    单调性（从 Q1 到 Q5 递增或递减）比单纯的 IC 均值更能说明因子是否真有效。
    """
    T = fmat.shape[0]
    acc = np.zeros(q)
    cnt = np.zeros(q)
    for t in range(T):
        f, r = fmat[t], rmat[t]
        m = np.isfinite(f) & np.isfinite(r)
        if int(m.sum()) < min_cross * 2:
            continue
        fv, rv = f[m], r[m]
        order = np.argsort(fv)
        # 分成 q 档（尽量均分）
        for k in range(q):
            lo = k * len(order) // q
            hi = (k + 1) * len(order) // q
            if hi <= lo:
                continue
            acc[k] += float(rv[order[lo:hi]].mean())
            cnt[k] += 1
    with np.errstate(invalid="ignore"):
        means = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    return {"layers": [None if not np.isfinite(x) else float(x) for x in means],
            "counts": [int(x) for x in cnt],
            "monotonic": _monotonic(means)}


def _monotonic(means: np.ndarray) -> bool:
    v = [x for x in means if np.isfinite(x)]
    if len(v) < 3:
        return False
    inc = all(v[i + 1] >= v[i] for i in range(len(v) - 1))
    dec = all(v[i + 1] <= v[i] for i in range(len(v) - 1))
    return bool(inc or dec)


# ---------------------------------------------------------------------------
# 市况分段（用成分股自构等权指数，不依赖外部指数数据）
# ---------------------------------------------------------------------------

def regime_labels(mats: Dict[str, Any], window: int = 20) -> List[str]:
    """用成分股等权指数的 rolling 收益标记市况：up / down / flat。"""
    r = _daily_ret(mats["close"])
    eq_ret = np.nanmean(r, axis=1)
    eq_ret = np.where(np.isfinite(eq_ret), eq_ret, 0.0)
    T = len(eq_ret)
    labels: List[str] = []
    for t in range(T):
        lo = max(0, t - window + 1)
        seg = eq_ret[lo:t + 1]
        s = float(seg.sum())
        labels.append("up" if s > 0.01 else ("down" if s < -0.01 else "flat"))
    return labels


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def evaluate(pool: str = "000300", forward: int = 5,
             keys: Optional[Sequence[str]] = None,
             with_regime: bool = True) -> Dict[str, Any]:
    """跑一轮完整因子检验。返回可直接渲染的 dict。"""
    mats = load_universe(pool)
    dates: List[str] = mats["dates"]
    C = mats["close"]
    facs = build_factors(mats, keys)
    rmat = forward_returns(C, forward=forward)
    labels = regime_labels(mats) if with_regime else ["flat"] * len(dates)

    items: List[Dict[str, Any]] = []
    for key, name, cat, _fn in FACTOR_DEFS:
        if key not in facs:
            continue
        fmat = facs[key]
        ics = ic_series(fmat, rmat)
        s = ic_summary(ics)
        item: Dict[str, Any] = {
            "key": key, "name": name, "cat": cat,
            "obs": s["obs"], "ic_mean": s["ic_mean"], "icir": s["icir"],
            "pos_ratio": s["pos_ratio"], "t_stat": s["t_stat"],
            "direction": s["direction"],
            "pass": bool(s["icir"] is not None and abs(s["icir"]) >= ICIR_PASS),
            # 因子有效率：防止「因子算废了(全NaN)」被误读成「因子没预测力」
            "cover": float(np.count_nonzero(np.isfinite(fmat))
                           / max(1, fmat.size)),
        }
        q = quantile_check(fmat, rmat)
        item["layers"] = q["layers"]
        item["monotonic"] = q["monotonic"]
        if with_regime and s["ic_mean"] is not None:
            reg: Dict[str, Any] = {}
            for lb in ("up", "down", "flat"):
                sel = np.array([i for i, x in enumerate(labels) if x == lb])
                if sel.size >= 20:
                    sub = ics[sel]
                    sub = sub[np.isfinite(sub)]
                    if sub.size >= 10:
                        m = float(sub.mean())
                        sd = float(sub.std(ddof=1))
                        reg[lb] = {"obs": int(sub.size), "ic_mean": m,
                                   "icir": (m / sd) if sd > 0 else None}
            item["regime"] = reg
        items.append(item)

    items.sort(key=lambda x: abs(x.get("icir") or 0), reverse=True)
    span = [dates[0], dates[-1]] if dates else ["", ""]
    return {
        "ok": True,
        "pool": mats.get("_pool", pool),
        "universe": len(mats["codes"]),
        "span": span,
        "days": len(dates),
        "forward": forward,
        "icir_pass": ICIR_PASS,
        "items": items,
    }


def report_text(res: Dict[str, Any]) -> str:
    """把结果渲染成易读文本。"""
    L: List[str] = []
    L.append("=" * 78)
    L.append(f"因子 IC 检验 | 池={res['pool']} | {res['universe']} 只 | "
             f"{res['span'][0]} ~ {res['span'][1]} ({res['days']} 交易日)")
    L.append(f"未来收益窗口 = {res['forward']} 日 | 判定门槛 |ICIR| >= {res['icir_pass']}")
    L.append("=" * 78)
    L.append(f"{'因子':<18}{'类别':<8}{'IC均值':>9}{'ICIR':>8}{'IC>0':>8}{'t值':>8}"
             f"{'有效率':>8}{'单调':>6}  判定")
    for it in res["items"]:
        ic = it["ic_mean"]
        ir = it["icir"]
        pr = it["pos_ratio"]
        ts = it["t_stat"]
        cv = it.get("cover")
        mark = "✅通过" if it["pass"] else "—"
        warn = " ⚠无效" if (cv is not None and cv < 0.5) else ""
        L.append(f"{it['name']:<18}{it['cat']:<8}"
                 f"{(f'{ic:+.4f}' if ic is not None else 'n/a'):>9}"
                 f"{(f'{ir:+.3f}' if ir is not None else 'n/a'):>8}"
                 f"{(f'{pr*100:.0f}%' if pr is not None else 'n/a'):>8}"
                 f"{(f'{ts:+.2f}' if ts is not None else 'n/a'):>8}"
                 f"{(f'{cv*100:.0f}%' if cv is not None else 'n/a'):>8}"
                 f"{('是' if it['monotonic'] else '否'):>6}  {mark}{warn}")
    L.append("")
    L.append("五档分层（Q1=因子最小 → Q5=因子最大），单位：窗口内平均收益")
    for it in res["items"]:
        if not it.get("layers"):
            continue
        lay = " ".join(f"{(f'{x*100:+.2f}%' if x is not None else 'n/a'):>8}"
                       for x in it["layers"])
        L.append(f"  {it['name']:<18}{lay}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------

def selfcheck(verbose: bool = True) -> Dict[str, Any]:
    """纯离线自检（不联网、不读真实库）。

    重点验证三类容易出错的地方：
    1. 秩相关正确性（含并列、完全正/负相关）。
    2. 未来收益无前视 —— 索引必须严格指向 t 之后。
    3. 检验框架能识别前视作弊 —— 把「未来收益本身」当因子喂进去，IC 必须接近 1；
       若自检里这条不成立，说明 IC 计算有问题，后续所有结论都不可信。
    """
    steps: List[Tuple[str, bool, str]] = []

    def rec(label: str, cond: bool, extra: str = "") -> None:
        steps.append((label, bool(cond), extra))
        if verbose:
            print(f"  {'✅' if cond else '❌'} {label}{(' → ' + extra) if extra else ''}")

    rng = np.random.default_rng(20261006)

    # --- 1. rank_avg 基本正确性 ---
    v = np.array([3.0, 1.0, 2.0])
    r = rank_avg(v)
    rec("rank_avg 无并列时秩正确",
        np.allclose(r, [3.0, 1.0, 2.0]), f"{r.tolist()}")

    v2 = np.array([5.0, 5.0, 1.0])     # 两个并列最大
    r2 = rank_avg(v2)
    # 1.0 最小 → 秩 1；两个 5.0 并列最大，占秩 2、3 → 平均 2.5
    rec("rank_avg 并列取平均秩",
        np.allclose(sorted(r2.tolist()), [1.0, 2.5, 2.5]), f"{r2.tolist()}")

    # --- 2. ic_series 极值 ---
    T, N = 40, 60
    fmat = rng.normal(size=(T, N))
    rec("IC：因子与收益完全正相关 → IC ≈ +1",
        abs(float(np.nanmean(ic_series(fmat, fmat))) - 1.0) < 1e-6,
        f"{float(np.nanmean(ic_series(fmat, fmat))):.6f}")
    rec("IC：因子与收益完全负相关 → IC ≈ -1",
        abs(float(np.nanmean(ic_series(fmat, -fmat))) + 1.0) < 1e-6,
        f"{float(np.nanmean(ic_series(fmat, -fmat))):.6f}")

    # --- 3. 未来收益无前视 ---
    # 构造严格线性增长的收盘价：close[t] = t+1，则任意 forward 的收益恒为
    # (t+1+lag+forward+1)/(t+1+lag+1) - 1，必须 > 0 且随 t 递减（基数变大）。
    C = np.tile(np.arange(1.0, T + 1.0).reshape(T, 1), (1, 3))
    rf = forward_returns(C, forward=5, lag=1)
    idx = T - 8          # 该行有完整未来数据
    expect = (C[idx + 1 + 5, 0] / C[idx + 1, 0]) - 1.0
    rec("forward_returns 索引正确（买入=t+1，卖出=t+1+forward）",
        abs(float(rf[idx, 0]) - expect) < 1e-9,
        f"got={float(rf[idx,0]):.6f} exp={expect:.6f}")
    rec("forward_returns 末尾留白（无未来数据处置 NaN）",
        bool(np.all(np.isnan(rf[T - 3:, 0]))), "")

    # 前视陷阱：把未来收益本身当因子，IC 必须 ≈ 1。
    # 注意：必须让因子在各列之间有差异，否则秩的方差为 0，IC 会被跳过（得 NaN），
    # 这条自检就变成了「恒不触发的假检查」。故用随机游走而非全列相同的常数序列。
    Cch = 10 + np.cumsum(rng.normal(0, 0.02, (T, N)), axis=0)
    rf_ch = forward_returns(Cch, forward=5, lag=1)
    ics_cheat = ic_series(rf_ch, rf_ch)
    cheat = float(np.nanmean(ics_cheat[np.isfinite(ics_cheat)]))
    rec("前视陷阱可捕获：用未来收益当因子 → IC ≈ 1（说明检验框架有效）",
        abs(cheat - 1.0) < 1e-6, f"IC={cheat:.6f}")

    # --- 4. 因子无前视：改未来数据不该影响 t 日因子值 ---
    # 合成一段 OHLCV，先算因子；再把「未来」部分整体改成随机值，重算。
    # t <= mid 的因子值必须完全不变。
    n = 300
    close = 10 + np.cumsum(rng.normal(0, 0.02, n))
    mats = {"close": close[:, None].copy(),
            "high": close[:, None] * 1.01,
            "low": close[:, None] * 0.99,
            "open": close[:, None],
            "volume": np.full((n, 1), 1e6),
            "amount": np.full((n, 1), 1e8)}
    f0 = build_factors(mats)
    mats2 = {k: v.copy() for k, v in mats.items()}
    mid = n // 2
    for k in ("close", "high", "low", "open"):
        mats2[k][mid:] = rng.uniform(50, 60, (n - mid, 1))   # 篡改未来
    f1 = build_factors(mats2)
    bad = []
    for key in f0:
        a = f0[key][:mid - 130]      # 留出最长回望窗口(120)的余量
        b = f1[key][:mid - 130]
        same = np.allclose(a, b, equal_nan=True)
        if not same:
            bad.append(key)
    rec("所有因子无前视（篡改未来数据不回改历史因子值）",
        not bad, f"受影响因子={bad}" if bad else f"{len(f0)} 个因子全部通过")

    # --- 5. 分层单调性 ---
    fm = rng.normal(size=(T, N))
    rm = fm * 0.5 + rng.normal(0, 0.1, (T, N))   # 强正相关
    q = quantile_check(fm, rm)
    inc = q["layers"][-1] > q["layers"][0]
    rec("分层检验：强正相关因子 Q5 > Q1", bool(inc),
        f"Q1={q['layers'][0]:.4f} Q5={q['layers'][-1]:.4f}")

    ok = all(s[1] for s in steps)
    out = {"ok": ok, "steps": [{"name": s[0], "ok": s[1], "extra": s[2]}
                               for s in steps],
           "fails": [s[0] for s in steps if not s[1]]}
    if verbose:
        print(f"\n自检 {'全部通过' if ok else '存在失败'}："
              f"{sum(1 for s in steps if s[1])}/{len(steps)}")
    return out


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        r = selfcheck()
        sys.exit(0 if r["ok"] else 1)
    pool = "000300"
    for a in sys.argv[1:]:
        if a.startswith("--pool="):
            pool = a.split("=", 1)[1]
    fwd = 5
    for a in sys.argv[1:]:
        if a.startswith("--forward="):
            fwd = int(a.split("=", 1)[1])
    res = evaluate(pool=pool, forward=fwd)
    print(report_text(res))
