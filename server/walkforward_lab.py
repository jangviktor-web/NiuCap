"""阶段4：walk-forward 跨牛熊样本外验证。

回答一个阶段3 绕不开的问题：**阶段3 在 2024-2026（恰逢牛市）上挑出的轮动参数，
放到更早的 2011-2023 还灵不灵？** 如果只在 2024-2026 暴赚、历史段跑输，那结论
就是过拟合，不是 alpha。

做法（标准 expanding walk-forward，杜绝前视）：
1. 全历史只加载一次；因子矩阵在全历史上**预计算**（窗口起点的信号因此天然有效，
   不需要再 warm-up —— 这正是给 rotate 加 warm_days 参数的原因）。
2. 把历史切成若干折：第 k 折用 [起点, 训练末] 调参（含因子方向 sign、调仓周期、
   持仓数、是否风控），再用 [训练末+1, 测试末] 这段**从未参与调参**的数据验证。
3. 每一折的因子方向都由该折**训练段**的 IC 符号重新决定（不把 2024-2026 的结论
   偷塞给历史段）。
4. 同时跑一个「固定原策略」基线（阶段3 的 sign/参数），看 retune 是否真的有帮助，
   还是策略本身在 OOS 就失效了。

重要方法学声明（见报告）：本工程 index_pools.json 是「当前」成分股，用当前 1000 只
回测 2011-2023 会系统性漏掉期间退市/被踢出的票 → 结论偏乐观（幸存者偏差）。这是
数据层面的硬约束，代码无法消除，只能在报告里点明。

依赖：numpy + factor_lab（IC/因子）+ rotation_lab（回测/合成）。
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 让本模块可直接 `python server/walkforward_lab.py` 运行（sibling 导入）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import factor_lab as fl
import rotation_lab as rot

FORWARD = 5                      # 与阶段2/3 一致的未来收益窗口
CANDIDATE_FACTORS = ("rev_5", "mom_60", "vol_surge")  # 阶段3 选定的三独立维度
ICIR_CONFIRM = 0.3               # 训练段 ICIR 达标才纳入该因子（否则中性剔除）
FIXED_SPECS = rot.FACTOR_SPECS   # 阶段3 固定基线（用于对照）


# ---------------------------------------------------------------------------
# 数据加载与切片（不修改 factor_lab / rotation_lab 的数据接口，只切片复用）
# ---------------------------------------------------------------------------

def load_full(pool: str = "000852") -> Dict[str, Any]:
    """加载全历史（已补到 ~2011），返回 factor_lab 的 mats 结构。"""
    return fl.load_universe(pool)


def slice_mats(mats: Dict[str, Any], a: int, b: int) -> Dict[str, Any]:
    """按日期索引 [a, b]（含端点）切片，保持字段矩阵形状、codes 不变。

    只有形状[0]==len(dates) 的数组（各 OHLCV 字段，形状 (T,N)）才按行切片；
    codes 等长度 N 的字段不切。
    """
    T = len(mats["dates"])
    out: Dict[str, Any] = {}
    for k, v in mats.items():
        if isinstance(v, np.ndarray) and v.ndim >= 1 and v.shape[0] == T:
            out[k] = v[a:b + 1]
        else:
            out[k] = v
    out["dates"] = mats["dates"][a:b + 1]
    return out


# ---------------------------------------------------------------------------
# 训练段：因子方向 + 参数调优（决定该折的策略形态，只用训练段数据）
# ---------------------------------------------------------------------------

def train_signs(facs: Dict[str, np.ndarray], rmat: np.ndarray,
                a: int, b: int) -> List[Tuple[str, int, float]]:
    """用训练段 [a,b] 的 IC 符号决定每个候选因子的方向；ICIR 未达标的剔除。"""
    specs: List[Tuple[str, int, float]] = []
    for key in CANDIDATE_FACTORS:
        if key not in facs:
            continue
        ics = fl.ic_series(facs[key][a:b + 1], rmat[a:b + 1])
        s = fl.ic_summary(ics)
        if s["icir"] is not None and abs(s["icir"]) >= ICIR_CONFIRM and \
                s["ic_mean"] is not None:
            specs.append((key, 1 if s["ic_mean"] > 0 else -1, 1.0))
    return specs


def tune_params(mats_win: Dict[str, Any], score_win: np.ndarray
                ) -> Tuple[int, int, bool]:
    """在训练窗口上网格搜索 (rebal, topk, use_filter)，以训练段夏普最大为准。

    固定 cost_on=True（实盘口径）。warm_days=0：因子已在全历史预计算，窗口
    起点信号有效，不参与 warm-up，避免白丢训练段头部的样本。
    """
    best: Optional[Tuple[float, int, int, bool]] = None
    for rebal in (10, 21, 42):
        for topk in (10, 20, 30):
            for use_filter in (True, False):
                r = rot.rotate(mats_win, score_win, rebal=rebal, topk=topk,
                               use_filter=use_filter, cost_on=True, warm_days=0)
                sh = r["stats"]["sharpe"]
                if sh is not None and (best is None or sh > best[0]):
                    best = (sh, rebal, topk, use_filter)
    if best is None:
        return (21, 20, True)
    return (best[1], best[2], best[3])


# ---------------------------------------------------------------------------
# 折的切分（expanding window：训练段始终从起点开始、测试段顺次前移）
# ---------------------------------------------------------------------------

def default_folds(dates: Sequence[str], train_min: int = 750,
                  test_len: int = 500) -> List[Tuple[int, int, int, int]]:
    """默认折：训练段从起点起、测试段 ~2 年（500 交易日）顺次前移，最后一段延伸到末尾。

    返回 [(a_train, b_train, a_test, b_test), ...]，每折测试段严格在训练段之后。
    """
    n = len(dates)
    folds: List[Tuple[int, int, int, int]] = []
    te = train_min + test_len
    while te < n:
        tr_end = te - test_len - 1
        ts_start = te - test_len
        ts_end = te - 1
        folds.append((0, tr_end, ts_start, ts_end))
        te += test_len
    if not folds:                       # 数据太短，退化为单折
        folds.append((0, max(0, n // 2 - 1), n // 2, n - 1))
    # 末折测试延伸到末尾，尽量用满历史
    last = folds[-1]
    folds[-1] = (last[0], last[1], last[2], n - 1)
    return folds


# ---------------------------------------------------------------------------
# 单折运行
# ---------------------------------------------------------------------------

def run_fold(full: Dict[str, Any], facs: Dict[str, np.ndarray],
             rmat: np.ndarray, fold: Tuple[int, int, int, int]
             ) -> Dict[str, Any]:
    a_tr, b_tr, a_te, b_te = fold
    dates = full["dates"]

    # 1) 训练段决定因子方向
    specs = train_signs(facs, rmat, a_tr, b_tr)
    signs_confirmed = bool(specs)
    if not specs:
        specs = list(FIXED_SPECS)       # 训练段无因子达标 → 退化为固定基线方向

    # 2) 全历史合成（方向由本折训练段决定），再切片
    score_full = rot.composite(facs, specs)
    tr_mats = slice_mats(full, a_tr, b_tr)
    score_tr = score_full[a_tr:b_tr + 1]
    rebal, topk, use_filter = tune_params(tr_mats, score_tr)

    # 3) 测试段（从未参与调参）验证
    te_mats = slice_mats(full, a_te, b_te)
    score_te = score_full[a_te:b_te + 1]
    r = rot.rotate(te_mats, score_te, rebal=rebal, topk=topk,
                   use_filter=use_filter, cost_on=True, warm_days=0)
    bench = rot.buyhold_benchmark(te_mats)
    s, b = r["stats"], bench["stats"]

    # 4) 固定基线对照：阶段3 原 sign/参数，同一测试段
    score_fixed = rot.composite(facs, FIXED_SPECS)
    rf = rot.rotate(te_mats, score_fixed[a_te:b_te + 1], rebal=21, topk=20,
                   use_filter=True, cost_on=True, warm_days=0)
    bf = rf["stats"]["total"]

    return {
        "span_train": [dates[a_tr], dates[b_tr]],
        "span_test": [dates[a_te], dates[b_te]],
        "signs_confirmed": signs_confirmed,
        "specs": [list(x) for x in specs],
        "params": {"rebal": rebal, "topk": topk, "use_filter": use_filter},
        "test_strat_total": s["total"],
        "test_bench_total": b["total"],
        "test_excess": s["total"] - b["total"],
        "test_strat_mdd": s["mdd"],
        "test_strat_sharpe": s["sharpe"],
        "test_fixed_total": bf,
        "test_fixed_excess": bf - b["total"],
        "beats_bench": bool(s["total"] > b["total"]),
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(pool: str = "000852", folds: Optional[List[Tuple[int, int, int, int]]] = None,
        forward: int = FORWARD) -> Dict[str, Any]:
    full = load_full(pool)
    C = full["close"]
    dates: List[str] = full["dates"]
    facs = fl.build_factors(full)
    rmat = fl.forward_returns(C, forward=forward)
    if folds is None:
        folds = default_folds(dates)

    out_folds: List[Dict[str, Any]] = []
    for f in folds:
        out_folds.append(run_fold(full, facs, rmat, f))

    n = len(out_folds)
    excess = [x["test_excess"] for x in out_folds]
    fixed_excess = [x["test_fixed_excess"] for x in out_folds]
    agg = {
        "n_folds": n,
        "beats": sum(1 for x in out_folds if x["beats_bench"]),
        "avg_excess": float(np.mean(excess)) if excess else None,
        "median_excess": float(np.median(excess)) if excess else None,
        "avg_fixed_excess": float(np.mean(fixed_excess)) if fixed_excess else None,
        "avg_strat_mdd": float(np.mean([x["test_strat_mdd"] for x in out_folds])),
    }
    return {
        "ok": True,
        "pool": full.get("_pool", pool),
        "universe": len(full["codes"]),
        "span": [dates[0], dates[-1]],
        "forward": forward,
        "folds": out_folds,
        "agg": agg,
        "survivorship_note": (
            "index_pools.json 为「当前」成分股，用其回测 2011-2023 会漏掉期间退市/"
            "被踢出的票，结论系统性偏乐观（幸存者偏差）。此偏差无法靠代码消除，仅供"
            "参考；真正确认需引入时点成分股数据。"),
    }


def report_text(res: Dict[str, Any]) -> str:
    L: List[str] = []
    a = res["agg"]
    L.append("=" * 80)
    L.append(f"Walk-Forward 样本外验证 | 池={res['pool']} | {res['universe']} 只 | "
             f"{res['span'][0]} ~ {res['span'][1]}")
    L.append(f"未来窗口={res['forward']}日 | 折数={a['n_folds']} | 判定：测试段累计是否跑赢等权基准")
    L.append("=" * 80)
    L.append(f"{'训练段':<24}{'测试段':<24}{'策略':>9}{'基准':>9}"
             f"{'超额':>8}{'回撤':>8}{'固定基线':>9}")
    for x in res["folds"]:
        L.append(f"{x['span_train'][0]}~{x['span_train'][1]}  "
                 f"{x['span_test'][0]}~{x['span_test'][1]}  "
                 f"{x['test_strat_total']*100:>+8.1f}%"
                 f"{x['test_bench_total']*100:>+8.1f}%"
                 f"{x['test_excess']*100:>+7.1f}pp"
                 f"{x['test_strat_mdd']*100:>+7.1f}%"
                 f"{x['test_fixed_total']*100:>+8.1f}%"
                 f"{'  (方向未确认)' if not x['signs_confirmed'] else ''}")
    L.append("")
    L.append(f"汇总：{a['beats']}/{a['n_folds']} 折跑赢基准 | "
             f"平均超额 {a['avg_excess']*100:+.1f}pp | "
             f"中位超额 {a['median_excess']*100:+.1f}pp | "
             f"平均回撤 {a['avg_strat_mdd']*100:.1f}%")
    L.append(f"固定基线（阶段3 原参数）平均超额：{a['avg_fixed_excess']*100:+.1f}pp")
    L.append("")
    L.append("⚠ " + res["survivorship_note"])
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 离线自检（不联网、不读真实库）
# ---------------------------------------------------------------------------

def selfcheck(verbose: bool = True) -> Dict[str, Any]:
    steps: List[Tuple[str, bool, str]] = []

    def rec(label: str, cond: bool, extra: str = "") -> None:
        steps.append((label, bool(cond), extra))
        if verbose:
            print(f"  {'✅' if cond else '❌'} {label}"
                  f"{(' → ' + extra) if extra else ''}")

    rng = np.random.default_rng(20261007)

    # --- 1. slice_mats 形状正确、dates 跟随 ---
    T, N = 120, 40
    mats = {"dates": [f"d{i:03d}" for i in range(T)],
            "codes": [f"c{i}" for i in range(N)],
            "close": rng.normal(size=(T, N)),
            "open": rng.normal(size=(T, N)),
            "high": rng.normal(size=(T, N)),
            "low": rng.normal(size=(T, N)),
            "volume": rng.normal(size=(T, N)),
            "amount": rng.normal(size=(T, N))}
    sm = slice_mats(mats, 10, 29)
    rec("slice_mats 行切片长度=端点差+1",
        sm["close"].shape[0] == 20 and len(sm["dates"]) == 20,
        f"close.shape={sm['close'].shape} dates={len(sm['dates'])}")
    rec("slice_mats codes 不被切片",
        sm["codes"] is mats["codes"] and len(sm["codes"]) == N)

    # --- 2. train_signs 能识别正负 IC 方向 ---
    facs = {"rev_5": np.zeros((T, N)), "mom_60": np.zeros((T, N)),
            "vol_surge": np.zeros((T, N))}
    # 构造：rev_5 与未来收益强正相关 → 应判 +；mom_60 强负相关 → 应判 -；
    # vol_surge 噪声 → ICIR 不达标被剔除
    base = rng.normal(size=(T, N))
    rmat = base * 0.6 + rng.normal(0, 0.1, (T, N))      # 与 rev_5 同向
    facs["rev_5"] = base.copy()
    facs["mom_60"] = -base.copy()
    facs["vol_surge"] = rng.normal(0, 0.05, (T, N))     # 近似噪声
    specs = train_signs(facs, rmat, 0, T - 1)
    signs = {k: g for k, g, _ in specs}
    rec("train_signs 识别 rev_5 为正向", signs.get("rev_5") == 1, str(signs))
    rec("train_signs 识别 mom_60 为负向", signs.get("mom_60") == -1)
    rec("train_signs 剔除噪声因子 vol_surge",
        "vol_surge" not in signs, f"保留={list(signs)}")

    # --- 3. rotate(warm_days=0) 窗口起点即参与，净值长度正确 ---
    score = np.tile(np.arange(N, dtype=float), (T, 1))
    r0 = rot.rotate(mats, score, rebal=20, topk=10,
                    use_filter=False, cost_on=False, warm_days=0)
    # rotate 的 equity：索引 0 = 首日基线 1.0，之后每交易日 append 一次 → 长度 = T
    rec("rotate(warm_days=0) 净值长度 = T",
        len(r0["equity"]) == T, f"len={len(r0['equity'])}")

    # --- 4. tune_params 返回合法三元组且偏好高夏普 ---
    # 构造一段有 alpha 的合成：score 顺序固定、收益正
    close = 10 + np.cumsum(rng.normal(0, 0.01, (T, N)), axis=0)
    m2 = dict(mats, close=close, high=close * 1.01, low=close * 0.99,
              open=close.copy())
    sc = np.tile(np.arange(N, dtype=float), (T, 1))
    rb, tk, uf = tune_params(m2, sc)
    rec("tune_params 返回合法 (rebal,topk,use_filter)",
        rb in (10, 21, 42) and tk in (10, 20, 30) and isinstance(uf, bool),
        f"({rb},{tk},{uf})")

    # --- 5. 无前视：篡改测试窗口之后的数据不影响测试段净值 ---
    a_te, b_te = 10, 29
    te_mats = slice_mats(mats, a_te, b_te)
    score_te = score[a_te:b_te + 1]
    r_pre = rot.rotate(te_mats, score_te, rebal=20, topk=10,
                       use_filter=False, cost_on=False, warm_days=0)
    # 把测试窗口之后的「未来」数据整体改掉
    mats2 = {k: (v.copy() if isinstance(v, np.ndarray) else v)
             for k, v in mats.items()}
    mats2["close"][b_te + 1:] = rng.uniform(50, 60, (T - (b_te + 1), N))
    te_mats2 = slice_mats(mats2, a_te, b_te)
    r_post = rot.rotate(te_mats2, score_te, rebal=20, topk=10,
                        use_filter=False, cost_on=False, warm_days=0)
    rec("无前视：篡改窗口之后数据不改变测试段净值",
        np.allclose(r_pre["equity"], r_post["equity"]), "")

    ok = all(s[1] for s in steps)
    out = {"ok": ok, "steps": [{"name": s[0], "ok": s[1], "extra": s[2]}
                               for s in steps],
           "fails": [s[0] for s in steps if not s[1]]}
    if verbose:
        print(f"\n自检 {'全部通过' if ok else '存在失败'}："
              f"{sum(1 for s in steps if s[1])}/{len(steps)}")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="000852")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        r = selfcheck()
        sys.exit(0 if r["ok"] else 1)
    res = run(pool=args.pool)
    print(report_text(res))
