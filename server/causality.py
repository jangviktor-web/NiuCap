"""因果性自动检查 —— 证明因子真的"只看过去"，而不是靠人肉 review 保证。

## 为什么需要它

`factor_lab` 里每个因子都声称"t 日的值只用到 t 及之前的数据"。这句话目前
靠两件事撑着：写代码时的自觉，和随机基线自检里那条"作弊 picker 得 100 分"
（那验证的是**基线**对前视敏感，不是**因子**没有前视）。

一旦某个因子不慎写成 `C[t+1:]` 或 `np.diff(C, prepend=...)` 的镜像版本，
IC 会变得异常漂亮，而现有流程里没有任何一处会报警。这类 bug 的特点是
**结果好看**，所以永远不会被人怀疑。

本模块把 TradingView-API 的做法搬过来并做了适配：对因子函数做
**截断重放**，比对"全量算"与"只给前 k 天算"在重叠区是否逐点相同。

## 一次检查里实际跑的三件事

因子函数是 `f(mats) -> (T,N)` 的纯函数，`mats` 是被整体传进去的矩阵。
所以前视只可能通过**读 m > k 的行**发生。据此设计三种攻击：

1. **确定性重放**（`mode="truncate"`）
   对 8 个等距切点 k，把 `mats` 截到 `[:k]` 重算，要求重叠区 `[:k']`
   （k' 是扣掉右侧 warmup 后的可比值）逐点相等。
   —— 抓 `C[t+1]`、`np.roll(C, -1)` 这类**越界读**。

2. **未来扰动**（`mode="perturb"`）
   把第 k 天之后的全部数值乘上 1.7 再算，要求第 k 天及之前一分不变。
   —— 抓"明明没越界，但归一化用了全样本均值/标准差"这类**统计量泄漏**
   （`(x - mean(x)) / std(x)` 是全样本口径，最典型）。

3. **重绘检查**（`repaint`）
   把"截断算"和"全量算"在同一位置的值相比，找"当时有、后来变了"。
   对纯函数矩阵因子，这一项恒等于第 1 项；单独列出来是为了**将来接上
   增量式信号函数**（比如策略里的滚动状态）时，判定语义已经就位。

三种都是**逐点逐股**比较，报告里给出具体坐标（日期、代码）而不是一句
"不一致"—— 定位不到点的报警等于没报。

## 已知局限（写在这里，免得被当成万灵药）

- **只查 t 日值是否偷看未来，不查 `forward_returns` 的 lag 口径**。后者
  已在 `newbie_baseline` 与 `factor_lab.forward_returns` 里各自钉死。
- **不覆盖随机数**。因子带随机初始化时每次结果都不同，本检查会误报；
  这类函数会被标 `nondeterministic=True` 并跳过，而不是给个假绿灯。
- **不检查"用了不应该用的字段"**（比如用 amount 算价格因子）。那属于业务
  口径，机器判不了。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

# 因子函数的统一签名：{字段名: (T,N) 矩阵} -> (T,N)
FactorFn = Callable[[Dict[str, Any]], np.ndarray]

# 矩阵字段（`mats` 里除 dates/codes/_pool 之外的全部 ndarray）
MAT_KEYS = ("open", "close", "high", "low", "volume", "amount")

DEFAULT_POINTS = 8          # 等距切点数
DEFAULT_TOL = 1e-9          # 逐点相对误差容限（float64 重算应当完全一致）
WARMUP_MIN = 5              # 右侧容忍区下限（覆盖 RSI 等需要若干根的指标）
WARMUP_MAX = 60             # 上限，避免把可比区压得太小反而漏检


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _slice_mats(mats: Dict[str, Any], n: int) -> Dict[str, Any]:
    """取前 n 天（含）的子矩阵，非矩阵键原样带过去。

    非矩阵键（codes/_pool 等）**必须原样保留** —— 截断的是时间，不是列。
    """
    out: Dict[str, Any] = {}
    for k, v in mats.items():
        if isinstance(v, np.ndarray) and v.ndim == 2:
            out[k] = v[:n]
        elif k == "dates":
            out[k] = list(v)[:n]
        else:
            out[k] = v
    return out


def _vals_equal(a: np.ndarray, b: np.ndarray, tol: float) -> bool:
    """两个标量逐点相等：NaN 对 NaN 也算相等（因子在大片区域返回 NaN 是常态）。

    必须把 NaN 当"相等"处理 —— 否则每个因子在 warmup 区都会判不一致，
    检查立刻变成全红噪声，人就会开始忽略它。
    """
    an, bn = math.isnan(a), math.isnan(b)
    if an and bn:
        return True
    if an != bn:
        return False
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) <= tol * scale


def _first_diff(a: np.ndarray, b: np.ndarray, tol: float,
                dates: Sequence[str], codes: Sequence[str]
                ) -> Optional[Dict[str, Any]]:
    """返回第一个不一致的坐标；全一致返回 None。"""
    if a.shape != b.shape:
        return {"kind": "shape", "got": list(a.shape), "want": list(b.shape)}
    bad = ~(np.isclose(a, b, rtol=tol, atol=tol, equal_nan=True))
    if not bad.any():
        return None
    t, j = np.argwhere(bad)[0]
    return {
        "kind": "value",
        "t": int(t),
        "date": dates[t] if t < len(dates) else "?",
        "code": codes[j] if j < len(codes) else "?",
        "full": None if not np.isfinite(a[t, j]) else float(a[t, j]),
        "trunc": None if not np.isfinite(b[t, j]) else float(b[t, j]),
        "n_bad": int(bad.sum()),
    }


def _warmup_right(full: np.ndarray, trunc: np.ndarray, tol: float) -> Tuple[int, int]:
    """估算两个函数各自的"右侧 warmup 长度"，返回 (warmup_full, warmup_trunc)。

    为什么需要：`_f_vol(mats, 60)` 在算 `[:k]` 时，第 k 天的值用的是
    `[k-59, k]`，与全量算的第 k 天**确实相同**；但 `np.nanstd` 的分母取决于
    窗口里非 NaN 的个数，如果序列左端不足 60 根，行为和全量算就不一样。

    实用做法：从右侧往前扫，找到第一个"连续一致"的起点，认为那个点之后都是
    受 warmup 影响的尾巴，不参与比较。宁可少比一段，也不要制造假阳性 ——
    一个会随便报警的检查，最后一定会被关掉。
    """
    T = min(full.shape[0], trunc.shape[0])
    if T <= 0:
        return 0, 0
    eq = np.isclose(full[:T], trunc[:T], rtol=tol, atol=tol, equal_nan=True).all(axis=1)
    n = 0
    for t in range(T - 1, -1, -1):
        if eq[t]:
            break
        n += 1
    return n, n


# ---------------------------------------------------------------------------
# 主检查
# ---------------------------------------------------------------------------

def check_factor(mats: Dict[str, Any], fn: FactorFn, key: str = "",
                 points: int = DEFAULT_POINTS, tol: float = DEFAULT_TOL,
                 warmup: Optional[int] = None) -> Dict[str, Any]:
    """对单个因子函数做截断重放 + 未来扰动，返回结构化结论。

    返回：
        {
          "key", "ok", "verdict",            # ok=False && verdict 说明原因
          "points": [{k, t_compare, warmup, ...}],
          "look_ahead": {...} | None,        # 截断重放发现的不一致（越界读）
          "repaint": {...} | None,           # 截断算与全量算不一致（语义同上）
          "leak": {...} | None,              # 未来扰动导致历史值变化（统计量泄漏）
          "deterministic": bool,
        }
    """
    dates: List[str] = list(mats.get("dates") or [])
    codes: List[str] = list(mats.get("codes") or [])
    T = len(dates)

    # 兜底：太短的序列上切点没有意义，直接说清楚而不是给个绿勾
    if T < 3 * WARMUP_MIN:
        return {"key": key, "ok": False, "look_ahead": None, "repaint": None,
                "leak": None, "points": [], "deterministic": None,
                "verdict": f"序列仅 {T} 天，不足以做截断重放（至少 {3 * WARMUP_MIN} 天）"}

    base = fn(_slice_mats(mats, T))
    if not isinstance(base, np.ndarray) or base.shape != (T, len(codes)):
        got = getattr(base, "shape", type(base).__name__)
        return {"key": key, "ok": False, "look_ahead": None, "repaint": None,
                "leak": None, "points": [], "deterministic": None,
                "verdict": f"返回值形状应为 {(T, len(codes))}，实得 {got}"}

    # 确定性先验：同输入算两次必须一样，否则后面所有比较都没有意义
    again = fn(_slice_mats(mats, T))
    if not np.isclose(base, again, rtol=tol, atol=tol, equal_nan=True).all():
        return {"key": key, "ok": False, "look_ahead": None, "repaint": None,
                "leak": None, "points": [], "deterministic": False,
                "verdict": "同一输入两次结果不同（函数内含随机性），因果检查不适用"}

    # 切点：等距分布在中后段，避开最前几根（那里 warmup 太长，比值没意义）
    lo, hi = WARMUP_MIN * 3, T - WARMUP_MIN
    if hi <= lo:
        lo, hi = 1, T - 1
    pts = sorted({int(round(lo + (hi - lo) * i / (points - 1)))
                  for i in range(points)}) if hi > lo else [T - 1]

    per_pt: List[Dict[str, Any]] = []
    look_ahead = None
    repaint = None

    for k in pts:
        # ── A. 未来扰动（**先做这一步**，它没有容忍区）
        #
        # 重要：扰动检查必须先于截断检查，且不带任何 warmup 容忍。
        # 曾经先做截断再扰动，结果三个故意写坏的因子全部通过 —— 因为右侧
        # warmup 一旦吞掉 40~60 天，`np.roll(C,-1)` 那种"整体前移一位"的
        # 错误在尾巴上恰好被跳过。扰动不改长度，不需要容忍区，是最干净的一刀。
        pert = {kk: (v.copy() if isinstance(v, np.ndarray) and v.ndim == 2 else v)
                for kk, v in mats.items()}
        for kk in MAT_KEYS:
            arr = pert.get(kk)
            if isinstance(arr, np.ndarray) and arr.ndim == 2 and arr.shape[0] > k:
                arr[k:] = arr[k:] * 1.7
        pert_note = ""
        try:
            ph = fn(pert)
        except Exception as e:
            ph, pert_note = None, f"扰动后算不出来：{type(e).__name__}"
        if isinstance(ph, np.ndarray) and ph.shape == base.shape:
            d = _first_diff(base[:k], ph[:k], tol, dates, codes)
            if d is not None:
                item = dict(d, k=k, kind="leak")
                per_pt.append({"k": k, "date": dates[k - 1] if k else None,
                               "compare_to": k, "warmup": 0,
                               "status": "历史值被未来数据改变",
                               "perturb": item})
                look_ahead = look_ahead or item
                continue
        elif ph is not None:
            pert_note = f"扰动后形状变了：{getattr(ph, 'shape', type(ph).__name__)}"

        # ── B. 截断重放：全量算的前 k 天 vs 只喂 k 天算的前 k 天
        trunc = fn(_slice_mats(mats, k))
        n_full, n_trunc = _warmup_right(base[:k], trunc, tol)
        eff = (warmup if warmup is not None else max(n_full, n_trunc, WARMUP_MIN))
        cmp_to = k - eff
        if cmp_to <= 0:
            per_pt.append({"k": k, "date": dates[k - 1] if k else None,
                           "compare_to": 0, "warmup": eff, "status": "跳过",
                           "perturb": pert_note})
            continue
        diff_trunc = _first_diff(base[:cmp_to], trunc[:cmp_to], tol, dates, codes)
        per_pt.append({
            "k": k, "date": dates[cmp_to - 1], "compare_to": cmp_to,
            "warmup": eff,
            "status": "一致" if diff_trunc is None else "不一致",
            "perturb": pert_note,
        })
        if diff_trunc is not None and look_ahead is None:
            # 对纯函数矩阵因子，"越界读"与"重绘"是同一件事；两个字段都填，
            # 是为将来接增量式信号函数时语义已经就位。
            look_ahead = dict(diff_trunc, k=k)
            repaint = dict(diff_trunc, k=k)

    ok = look_ahead is None
    return {
        "key": key, "ok": ok, "deterministic": True,
        "points": per_pt,
        "look_ahead": look_ahead,
        "repaint": repaint,
        # leak 与 look_ahead 共用一个槽位，用 kind 区分来源
        "leak": look_ahead if (look_ahead or {}).get("kind") == "leak" else None,
        "verdict": "只看过去（截断重放与未来扰动均无变化）" if ok else _why(look_ahead),
    }


def _why(d: Optional[Dict[str, Any]]) -> str:
    if not d:
        return "通过"
    if d.get("kind") == "leak":
        return (f"用了未来数据：把 {d.get('date')} 之后的价格改掉，"
                f"{d.get('date')} 当天的值跟着变了（{d.get('n_bad')} 个格子）")
    if d.get("kind") == "shape":
        return f"截断后返回形状变了：{d.get('want')} → {d.get('got')}"
    return (f"{d.get('date')} {d.get('code')} 处不一致："
            f"全量算 {d.get('full')} vs 截断算 {d.get('trunc')}"
            f"（共 {d.get('n_bad')} 个格子）")


def check_all(mats: Dict[str, Any],
              factors: Dict[str, FactorFn],
              points: int = DEFAULT_POINTS,
              tol: float = DEFAULT_TOL) -> Dict[str, Any]:
    """批量检查一组因子，返回 {"ok", "passed", "failed", "items", "summary"}。

    契约与 `factor_lab.availability` 一致：**宁可报红也不给假绿**。
    任何一个因子失败，整体 `ok=False`，并在 `summary` 里直接点名。
    """
    items = [check_factor(mats, fn, key=k, points=points, tol=tol)
             for k, fn in factors.items()]
    failed = [it for it in items if not it["ok"]]
    return {
        "ok": not failed,
        "total": len(items),
        "passed": len(items) - len(failed),
        "failed": len(failed),
        "items": items,
        "summary": ("全部通过因果性检查" if not failed else
                    "以下因子未通过：" + "、".join(
                        f"{it['key']}（{it['verdict']}）" for it in failed)),
    }
