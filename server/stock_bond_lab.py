#!/usr/bin/env python3
"""股债温度：沪深300 相对国债，是贵了还是便宜了
================================================

回答一个问题：**当前沪深300 相对债券，处在历史区间的什么位置？**

三个必须写在最前面的诚实声明
------------------------------
1. **债券腿用国债ETF 价格（sh511010），不是国债收益率。**
   没有任何免费源能给出 2005 年起的完整国债收益率曲线（实测：中债源
   只到 2021-01 就停更、东财三个报表全返空、push2his 直接 403）。
   因此本指标与万得「股债利差」**方向相反、刻度不同，数值不可对读**：
   收益率涨=债券跌，ETF 涨=债券涨；纵轴是 z 值不是基点。
   界面上不许出现「股债利差」四个字，那是另一个东西。

2. **必须用滚动 z，不能用全样本均值。**
   债券 ETF 13 年 +40.78% 是票息累积（约 2.7%/年），不是行情。直接相除
   得到的比值带结构性趋势，用全样本均值画通道等于拿未来数据画今天的线。
   滚动 z 的每个点只用当日为止的 W 天，无前视。

3. **样本内结论，不产出买卖信号。**
   窗口长度 500 是按 IC 挑出来的，样本外未验；250 日前瞻下相邻样本重叠
   249 天，名义 t=−20.8 去重叠后只剩 −4.93。报告 IC 时两个 n 都要给。

方法
----
- 数据源：sources.eltdx_source.get_bars_batch（批量），**adjust='none' 强制**。
  ⚠️ 它默认 qfq，会把 511010 的历史整体压低 5.4% 且 13 年里阶梯式变化，
  直接污染 z —— 实测 qfq 下 z=−0.85，none 下 z=−0.55，差 0.3 且无法事后校正。
  备源 datasource.kline_sina（实测 z 值一致）。腾讯对该组 ETF 返空。
- 对齐：两腿交易日取交集，任一腿缺数据即整段作废，不补不插值。
- 指标：lr = ln(p_stock / p_bond)；z = (lr - MA_W) / SD_W（ddof=1）
- 通道：MA ± 2·SD（W 默认 500，可切 250/750/1200）
- 分区：z < -1.5 便宜 / -1.5~1.5 通道内 / > 1.5 贵

为什么窗口默认 500
-------------------
在 3288 个共同交易日上试了四个窗口，±2σ 命中率（正态理论各 2.3%）：

    W=250   上轨 9.8%  下轨 7.6%   极值 −4.20/+4.93   z 长期趴在通道外
    W=500   上轨 9.6%  下轨 4.6%   极值 −2.63/+3.25   信号最强
    W=750   上轨 3.3%  下轨 0.7%   极值 −2.41/+2.78   更接近理论但 IC 更弱
    W=1200  上轨 6.7%  下轨 2.5%   极值 −2.45/+3.18

命中率都不是 2.3%，说明比值非正态且有自相关 —— 这一条要如实告诉用户：
「触轨」不是稀有事件，实测约 10% 的交易日都在上轨上方。选 500 是因为
它的 250 日前瞻 IC 最高（−0.381，其余三档 −0.31 ~ −0.40），代价是假信号多，
所以窗口做成可切，让用户自己看敏感性。

ponytail: 不做信号、不做 AI 解读。这里的结论是算术（z 在哪、分位多少、
三区历史均值多少），AI 插进来只会把确定数字写成含糊话。要加 AI，先把
样本外验证做完。
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

STOCK_CODE = "sh000300"
BOND_CODE = "sh511010"
BOND_NAME = "国债ETF国泰"

WINDOWS: Tuple[int, ...] = (250, 500, 750, 1200)
DEFAULT_WINDOW = 500
BAND_SIGMA = 2.0
ZONE = 1.5          # |z| 超过它才算「贵/便宜」，与 ±2σ 通道线错开，留出观察带
FWD_HORIZONS: Tuple[int, ...] = (60, 120, 250)
IC_HORIZON = 250

# 去重叠重采样间隔：250 日前瞻意味着相邻样本重叠 249 天，t 值因此虚高。
# 每 20 个交易日取 1 个点，把有效样本从 2289 压到 127，得到诚实的 t。
DEDUP_STEP = 20

# 前瞻收益最少样本数：低于它不给 IC（几个点算出的 r 接近 ±1，t 值上千）
MIN_FWD_N = 20


class _NoData(Exception):
    """两只标的没有足够的共同交易日。给上层一个明确的失败原因。"""


# ---------------------------------------------------------------- 取数


def _bars_via_eltdx() -> Optional[Dict[str, Dict[str, float]]]:
    """主源：通达信批量取数，**强制不复权**。

    返回 {'stock': {date: close}, 'bond': {date: close}}；取不到返回 None。
    """
    try:
        from sources.eltdx_source import available, get_bars_batch
    except Exception:
        return None
    if not available():
        return None
    try:
        d = get_bars_batch([STOCK_CODE, BOND_CODE], count=20000, adjust="none")
    except Exception:
        return None
    out = {}
    for key, code in (("stock", STOCK_CODE), ("bond", BOND_CODE)):
        rows = d.get(code) or []
        m = {str(r["date"])[:10]: float(r["close"]) for r in rows if r.get("close")}
        if len(m) < 300:
            return None
        out[key] = m
    return out


def _bars_via_sina() -> Optional[Dict[str, Dict[str, float]]]:
    """备源：新浪。eltdx 不可用时接管，实测两者算出的 z 完全一致。"""
    try:
        import datasource as ds
    except Exception:
        return None
    out = {}
    for key, code, cnt in (("stock", STOCK_CODE, 6000), ("bond", BOND_CODE, 4000)):
        try:
            rows = ds.kline_sina(code, count=cnt)
        except Exception:
            return None
        m = {str(r["date"])[:10]: float(r["close"]) for r in rows if r.get("close")}
        if len(m) < 300:
            return None
        out[key] = m
    return out


def load_series(force_source: Optional[str] = None) -> Dict[str, Any]:
    """取两腿收盘价并对齐。返回 {dates, stock, bond, source, start, end}。

    没有共同区间时抛 _NoData，带上两边各自的起止日 —— 让用户看到是
    哪一条腿没数，而不是一个笼统的「数据不可用」。
    """
    order = [force_source] if force_source else ["eltdx", "sina"]
    got, source = None, ""
    for src in order:
        got = _bars_via_eltdx() if src == "eltdx" else _bars_via_sina()
        if got:
            source = src
            break
    if not got:
        raise _NoData("通达信与新浪均未取到行情")

    s, b = got["stock"], got["bond"]
    dates = sorted(set(s) & set(b))
    if len(dates) < 60:
        raise _NoData(
            f"共同交易日仅 {len(dates)} 天"
            f"（沪深300 {min(s)}~{max(s)}，{BOND_NAME} {min(b)}~{max(b)}）")
    return {
        "dates": dates,
        "stock": np.array([s[d] for d in dates], dtype=float),
        "bond": np.array([b[d] for d in dates], dtype=float),
        "source": source,
        "start": dates[0],
        "end": dates[-1],
    }


# ---------------------------------------------------------------- 指标


def rolling_z(x: np.ndarray, w: int) -> np.ndarray:
    """滚动 z 值（ddof=1）。前 w-1 个点为 NaN —— 不把它们填 0。

    填 0 等于宣布「历史前两年比值等于滚动均值」，那是编数据。
    """
    n = len(x)
    z = np.full(n, np.nan)
    if w < 2 or n < w:
        return z
    cs = np.concatenate([[0.0], np.cumsum(x)])
    cs2 = np.concatenate([[0.0], np.cumsum(x * x)])
    for i in range(w - 1, n):
        seg = x[i - w + 1:i + 1]
        mu = cs[i + 1] - cs[i - w + 1]
        mu /= w
        var = (cs2[i + 1] - cs2[i - w + 1]) / w - mu * mu
        var *= w / (w - 1)          # 无偏：总体方差 → 样本方差
        if var <= 0:
            continue
        z[i] = (x[i] - mu) / float(np.sqrt(var))
    return z


def zone_of(z: Optional[float]) -> str:
    """三区判定。边界归属写死在这里，前端不许自己再判一遍。"""
    if z is None or not np.isfinite(z):
        return "na"
    if z > ZONE:
        return "expensive"
    if z < -ZONE:
        return "cheap"
    return "channel"


ZONE_TEXT = {"cheap": "便宜", "channel": "通道内", "expensive": "贵", "na": "无数据"}


def _t_of(r: float, n: int) -> float:
    if n < 3 or not np.isfinite(r) or abs(r) >= 1:
        return 0.0
    return float(r * np.sqrt((n - 2) / (1 - r * r)))


def forward_stats(lr_unused: np.ndarray, stock: np.ndarray, bond: np.ndarray,
                  z: np.ndarray, horizon: int) -> Dict[str, Any]:
    """z 与「之后 horizon 日 股票减债券 的对数收益」的关系。

    同时返回名义样本与去重叠样本的 IC/t —— 只报大的那个就是误导。
    """
    n = len(z)
    xs: List[float] = []
    ys: List[float] = []
    for i in range(n - horizon):
        if not np.isfinite(z[i]) or stock[i] <= 0 or bond[i] <= 0:
            continue
        rel = np.log(stock[i + horizon] / stock[i]) - np.log(bond[i + horizon] / bond[i])
        xs.append(float(z[i]))
        ys.append(float(rel))
    # 少于 20 个样本不给 IC：几个点算出的相关系数接近 ±1，t 值上千，
    # 那不是在给结论，是在造数据。宁可报「样本不足」。
    if len(xs) < MIN_FWD_N:
        return {"ok": False, "horizon": horizon, "n": len(xs)}
    xa, ya = np.array(xs), np.array(ys)
    r = float(np.corrcoef(xa, ya)[0, 1]) if xa.std() > 0 and ya.std() > 0 else 0.0

    # 去重叠：隔 DEDUP_STEP 取一个，压掉自相关带来的 t 值虚高
    xd, yd = xa[::DEDUP_STEP], ya[::DEDUP_STEP]
    rd = float(np.corrcoef(xd, yd)[0, 1]) if len(xd) > 3 and xd.std() > 0 else 0.0

    out = {"ok": True, "horizon": horizon, "n": len(xa),
           "ic": r, "ic_t": _t_of(r, len(xa)),
           "n_eff": len(xd), "ic_eff": rd, "ic_eff_t": _t_of(rd, len(xd)),
           "zones": {}}
    for name, mask in (("cheap", xa < -ZONE),
                       ("channel", (xa >= -ZONE) & (xa <= ZONE)),
                       ("expensive", xa > ZONE)):
        sel = ya[mask]
        out["zones"][name] = {
            "n": int(sel.size),
            "mean": float(sel.mean()) if sel.size else None,
            "median": float(np.median(sel)) if sel.size else None,
        }
    return out


def evaluate(window: int = DEFAULT_WINDOW,
             force_source: Optional[str] = None) -> Dict[str, Any]:
    """主入口。返回可直接下发给前端的完整结构。"""
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    d = load_series(force_source)
    dates, stock, bond = d["dates"], d["stock"], d["bond"]
    lr = np.log(stock / bond)
    z = rolling_z(lr, window)
    valid = np.isfinite(z)

    if not valid.any():
        raise _NoData(f"共同交易日 {len(dates)} 天不足窗口 {window} 天")

    # 序列：只下有效点（前 window-1 个 z 是 NaN，画不出来也没用）
    series = []
    for i in range(len(dates)):
        if not valid[i]:
            continue
        series.append({"date": dates[i],
                       "stock": round(float(stock[i]), 2),
                       "bond": round(float(bond[i]), 3),
                       "lr": round(float(lr[i]), 4),
                       "z": round(float(z[i]), 4)})

    zz = z[valid]
    cur = float(zz[-1])
    last_i = int(np.where(valid)[0][-1])
    stats: Dict[str, Any] = {
        "n_days": len(dates),
        "n_valid": int(valid.sum()),
        "hit_upper": float((zz > BAND_SIGMA).mean()),
        "hit_lower": float((zz < -BAND_SIGMA).mean()),
        "z_min": float(zz.min()),
        "z_max": float(zz.max()),
    }

    fwd = [forward_stats(lr, stock, bond, z, h) for h in FWD_HORIZONS]
    for f in fwd:
        if f.get("ok") and f["horizon"] == IC_HORIZON:
            stats["ic"] = f["ic"]
            stats["ic_t"] = f["ic_t"]
            stats["n_eff"] = f["n_eff"]
            stats["ic_eff"] = f["ic_eff"]
            stats["ic_eff_t"] = f["ic_eff_t"]

    # 当前一行的滚动均值/标准差：只取末端 W 天，和图上最后一段一致
    seg = lr[max(0, last_i - window + 1):last_i + 1]
    now = {
        "z": round(cur, 4),
        "zone": zone_of(cur),
        "zone_text": ZONE_TEXT[zone_of(cur)],
        "pct_rank": round(float((zz < cur).mean()), 4),
        "ma": round(float(seg.mean()), 4),
        "sd": round(float(seg.std(ddof=1)), 4),
        "lr": round(float(lr[last_i]), 4),
        "as_of": dates[last_i],
    }
    # 近 20 日两腿各自的涨跌（给「最近是谁在动」一个参照）
    if last_i >= 20:
        now["stock_ret_20d"] = round(float(stock[last_i] / stock[last_i - 20] - 1), 4)
        now["bond_ret_20d"] = round(float(bond[last_i] / bond[last_i - 20] - 1), 4)

    return {
        "ok": True,
        "target": {"stock": STOCK_CODE, "bond": BOND_CODE, "bond_name": BOND_NAME},
        "source": d["source"],
        "start": d["start"],
        "end": d["end"],
        "window": window,
        "band_sigma": BAND_SIGMA,
        "zone_threshold": ZONE,
        "series": series,
        "now": now,
        "stats": stats,
        "fwd": fwd,
        "caveats": [
            "债券腿是国债ETF价格，不是国债收益率——与万得「股债利差」方向相反、刻度不同，数值不可对读",
            f"起点 {d['start']} 由 {BOND_NAME} 上市日决定，不含 2008 金融危机",
            f"±2σ 实际命中率上轨 {100 * stats['hit_upper']:.1f}% / 下轨 "
            f"{100 * stats['hit_lower']:.1f}%，远高于正态的 2.3%，「触轨」不是稀有事件",
            "窗口长度按样本内 IC 选定，样本外未验；250 日前瞻去重叠后有效样本仅 "
            f"{stats.get('n_eff', '—')} 个",
            "本面板只做诊断，不产出买卖信号",
        ],
    }


def _z_with_adjust(adjust: str, window: int = DEFAULT_WINDOW) -> Optional[float]:
    """用指定复权口径算末点 z。**只给自检用**——主链路固定 adjust='none'。

    存在的意义是让「口径被改回默认」这件事可被机器检出：qfq 与 none 的 z
    恒定相差约 0.32，这个差值不随日期/行情变化，是比 z 本身更可靠的不变量。
    """
    try:
        from sources.eltdx_source import available, get_bars_batch
    except Exception:
        return None
    if not available():
        return None
    try:
        d = get_bars_batch([STOCK_CODE, BOND_CODE], count=20000, adjust=adjust)
    except Exception:
        return None
    s = {str(r["date"])[:10]: float(r["close"])
         for r in (d.get(STOCK_CODE) or []) if r.get("close")}
    b = {str(r["date"])[:10]: float(r["close"])
         for r in (d.get(BOND_CODE) or []) if r.get("close")}
    dates = sorted(set(s) & set(b))
    if len(dates) < window + 2:
        return None
    lr = np.log(np.array([s[d] for d in dates]) / np.array([b[d] for d in dates]))
    z = rolling_z(lr, window)
    if not np.isfinite(z[-1]):
        return None
    return float(z[-1])


# ---------------------------------------------------------------- 自检


def selfcheck() -> int:
    fails = 0

    def ok(name: str, cond: bool, detail: str = "") -> None:
        nonlocal fails
        if cond:
            print(f"  PASS  {name}")
        else:
            fails += 1
            print(f"  FAIL  {name}  {detail}")

    # 1. 等速双涨：z 必须约等于 0。真实数据里没有这种情形，
    #    但它是「比值去趋势」这件事的数学基准，破了就说明公式写错。
    #    容差取 1e-6 而不是机器精度：累积和求均值再减，浮点会留下 ~1e-7 残差，
    #    这是加法顺序的锅，不是逻辑错。要求 1e-9 只会得到一个永远红的断言。
    n = 400
    a = 1000 * np.exp(0.0003 * np.arange(n))
    b = 100 * np.exp(0.0003 * np.arange(n))
    z = rolling_z(np.log(a / b), 100)
    ok("等速双涨 → z 恒为 0", np.nanmax(np.abs(z)) < 1e-6,
       f"max|z|={np.nanmax(np.abs(z)):.2e}")

    # 2. 股票单边暴涨 30%：最后的 z 必须显著为正
    a2 = a.copy()
    a2[-1] *= 1.30
    z2 = rolling_z(np.log(a2 / b), 100)
    ok("股票暴涨 30% → z > 2", np.isfinite(z2[-1]) and z2[-1] > BAND_SIGMA,
       f"z={z2[-1]:.2f}")

    # 3. 样本不足：不抛异常、不返回垃圾，整段 NaN
    z3 = rolling_z(np.log(a[:50] / b[:50]), 100)
    ok("样本 < 窗口 → 全 NaN", np.isnan(z3).all())

    # 4. 窗口 = 1 或 0 不能除零崩掉
    ok("窗口退化不崩", np.isnan(rolling_z(np.arange(10.0), 1)).all()
       and np.isnan(rolling_z(np.arange(10.0), 0)).all())

    # 5. 分区边界归属明确、不留空隙
    ok("z=+1.5 归通道内（不含）", zone_of(1.5) == "channel")
    ok("z=+1.5001 归贵", zone_of(1.5001) == "expensive")
    ok("z=-1.5 归通道内", zone_of(-1.5) == "channel")
    ok("z=-1.5001 归便宜", zone_of(-1.5001) == "cheap")
    ok("z=NaN 归 na", zone_of(float("nan")) == "na")
    ok("z=inf 归 na", zone_of(float("inf")) == "na")

    # 6. 截断序列的 z 与全长序列的同一位置必须逐位相同。
    #    这是「无前视」的机器证明：多给了末日数据，历史 z 不许变。
    full = rolling_z(np.log(a / b) + np.sin(np.arange(n) * 0.1), 100)
    cut = rolling_z(np.log(a[:350] / b[:350]) + np.sin(np.arange(350) * 0.1), 100)
    ok("无前视：截断不影响已有点", np.allclose(full[:350], cut, equal_nan=True))

    # 7. 前瞻收益在序列末端返回可判定的结果，不越界。
    #    300 个点、250 日前瞻 → 50 个样本，够算 IC 但不够去重叠（只剩 3 个），
    #    此时必须仍报 ok=True 并把 n_eff 如实写出来，不能因为去重叠样本太少就
    #    整段判死——那会把「样本偏少」说成「没有数据」。
    zs = np.linspace(-2, 2, 300)
    st = np.linspace(100, 120, 300)
    bd = np.linspace(100, 110, 300)
    fs = forward_stats(None, st, bd, zs, 250)
    ok("前瞻：样本偏少仍可算，n_eff 如实上报",
       fs.get("ok") is True and fs["n"] == 50 and fs["n_eff"] < 10, str(fs.get("n_eff")))

    # 真正不够（样本过少）时必须报 ok=False，而不是拿几个点算个 IC 出来。
    # 300 点序列截到 268 → 名义 n=18 < MIN_FWD_N(20)，正好卡在门外。
    fs0 = forward_stats(None, st[:268], bd[:268], zs[:268], 250)
    ok(f"前瞻：样本 <{MIN_FWD_N} 报 ok=False",
       fs0.get("ok") is False and fs0.get("n") == 18, str(fs0))

    zs2 = np.linspace(-2, 2, 600)
    st2 = np.linspace(100, 100, 600)
    bd2 = np.linspace(100, 100, 600)
    fs2 = forward_stats(None, st2, bd2, zs2, 250)
    ok("前瞻：两腿不动 → IC 为 0 而非崩", fs2.get("ok") and abs(fs2["ic"]) < 1e-9,
       str(fs2.get("ic")))

    # 8. 真实数据：复权口径不得被改回 qfq。
    #
    #    这里**不能**断言 z 等于某个固定值。z 的末点跟着当日盘中价走，
    #    10-21 盘中 z=−0.55、10-22 盘中就成了 −0.74（差 0.19），而 qfq 与 none
    #    的差恒为 0.32。用固定值判定会把「盘中价正常变动」误报成「口径被改」，
    #    等于让断言天天变红、最后没人看它。
    #
    #    真正的不变量是**口径差**：同一时点 qfq 与 none 相差 0.30~0.35。
    #    断言这个区间，既不受盘中价格影响，又能在有人改回默认时立刻报红。
    try:
        r_none = evaluate(window=500, force_source="eltdx")
        r_qfq = _z_with_adjust("qfq")
        zc = r_none["now"]["z"]
        if r_qfq is None:
            ok("真实数据 复权口径差（防 qfq）", False, "qfq 取数失败，无法比对")
        else:
            gap = zc - r_qfq
            ok("真实数据 qfq 与 none 相差 0.30~0.35（防改回复权默认）",
               0.28 <= gap <= 0.38,
               f"none={zc:.3f} qfq={r_qfq:.3f} 差={gap:.3f}（正常应约 0.32）")
        ok("真实数据 z(500) 落在合理区间",
           -3.5 <= zc <= 3.5 and r_none["now"]["zone"] in ("cheap", "channel", "expensive"),
           f"z={zc}")
        ok("真实数据 共同交易日 ≥ 3288", r_none["stats"]["n_days"] >= 3288,
           str(r_none["stats"]["n_days"]))
        ok("真实数据 序列长度 = 有效点数",
           len(r_none["series"]) == r_none["stats"]["n_valid"])
        ok("真实数据 序列无 NaN",
           all(s["z"] is not None for s in r_none["series"]))
        ok("真实数据 IC 为负（股票越贵之后越弱）",
           r_none["stats"].get("ic", 0) < -0.2, str(r_none["stats"].get("ic")))
        ok("真实数据 起点 ≥ 2013", r_none["start"][:4] >= "2013", r_none["start"])

        # 去重叠样本必须真的少于名义样本。原写法拿 stats["n"] 比，可 stats 里根本没这个键
        # （只有 n_valid），get 回落到 0 后条件恒假，再挂个 `or True` 让它恒真——
        # 这条断言当时什么都测不到。名义 n 在 fwd 里，按 horizon 取 250 日那条。
        #
        # 必须写 `n_eff < n` 这个**严格**不等式。只写「n_eff 落在 n/DEDUP_STEP ~ n 之间」
        # 的话，n_eff == n（完全没去重叠）也落在区间内——反向验证时确实 True 了。
        # 上界的 `<= 2*DEDUP_STEP` 一起卡死：隔 20 取一个，n_eff 应该正好是 n//20 附近，
        # 只写 1 倍余量时 n_eff=2000（等于隔 1 个取一个）也会过。
        f250 = next((f for f in r_none["fwd"] if f["horizon"] == IC_HORIZON), None)
        ok("真实数据 去重叠有效样本 = n/20 上下（DEDUP_STEP 生效）",
           f250 is not None and f250.get("ok")
           and f250["n_eff"] < f250["n"]
           and f250["n"] <= f250["n_eff"] * DEDUP_STEP <= f250["n"] * 2,
           f'n_eff={f250.get("n_eff")} n={f250.get("n")}' if f250 else "缺 250 日样本")

        # 去重叠后 t 值必须缩水但仍显著。这条是「重叠样本把 t 吹大」的现场证据：
        # 名义 t≈-21、去重叠 t≈-5，两者若接近反说明去重叠没生效。
        ok("真实数据 去重叠 t 明显小于名义 t（重叠导致虚高）",
           f250 is not None and f250.get("ok")
           and abs(f250["ic_eff_t"]) < abs(f250["ic_t"]) / 2
           and abs(f250["ic_eff_t"]) > 2.0,
           f't={f250["ic_t"]:+.1f} → t_eff={f250["ic_eff_t"]:+.1f}' if f250 else "—")

        # 触轨率是页面 caveat 里写死的数字。它偏离正态 2.3% 这件事本身就是要告诉用户的
        # 结论，所以断言它「明显高于 2.3%」，避免有人日后把这条 caveat 改回正态假设。
        st = r_none["stats"]
        ok("真实数据 触轨率显著高于正态 2.3%（caveat 成立）",
           st["hit_upper"] > 0.05 and st["hit_lower"] > 0.02,
           f'上 {100 * st["hit_upper"]:.1f}% / 下 {100 * st["hit_lower"]:.1f}%')
    except _NoData as e:
        ok("真实数据（网络）", False, f"取数失败：{e}")
    except Exception as e:                      # noqa: BLE001
        ok("真实数据", False, f"{type(e).__name__}: {e}")

    print(f"\n{'OK' if fails == 0 else 'FAILED'} — {fails} failed")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="股债温度（沪深300 × 国债ETF）")
    ap.add_argument("--selfcheck", action="store_true", help="跑自检")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW, choices=WINDOWS)
    ap.add_argument("--source", default=None, choices=["eltdx", "sina"])
    ap.add_argument("--json", action="store_true", help="打印完整 JSON（不含 series）")
    a = ap.parse_args()
    if a.selfcheck:
        return 1 if selfcheck() else 0
    try:
        r = evaluate(window=a.window, force_source=a.source)
    except _NoData as e:
        print(f"取数失败：{e}")
        return 2
    if a.json:
        import json
        slim = dict(r)
        slim["series"] = f"<{len(r['series'])} points>"
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        return 0
    now = r["now"]
    print(f"{r['target']['stock']} × {r['target']['bond']}（{r['target']['bond_name']}）"
          f"  源={r['source']}  {r['start']} ~ {r['end']}")
    print(f"窗口 {r['window']}  {now['as_of']}")
    print(f"z = {now['z']:+.2f}   {now['zone_text']}   分位 {100 * now['pct_rank']:.1f}%")
    print(f"ln(股/债) = {now['lr']:.4f}   MA = {now['ma']:.4f}   SD = {now['sd']:.4f}")
    print(f"触轨率 上轨 {100 * r['stats']['hit_upper']:.1f}% / "
          f"下轨 {100 * r['stats']['hit_lower']:.1f}%  （正态理论 2.3%）")
    for f in r["fwd"]:
        if not f.get("ok"):
            continue
        zs = f["zones"]
        print(f"  {f['horizon']:3d}日  IC={f['ic']:+.3f} (t={f['ic_t']:+.1f}, "
              f"n={f['n']})  去重叠 IC={f['ic_eff']:+.3f} "
              f"(t={f['ic_eff_t']:+.1f}, n={f['n_eff']})")
        for nm in ("cheap", "channel", "expensive"):
            z = zs[nm]
            if z["mean"] is None:
                continue
            print(f"       {ZONE_TEXT[nm]:4s} n={z['n']:4d} "
                  f"均值 {100 * z['mean']:+6.1f}%  中位 {100 * z['median']:+6.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
