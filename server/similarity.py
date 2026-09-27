"""
形态相似度匹配（借鉴 a-share-quant-selector 的 B1 思路，按本项目实测重设计）

## ⚠️ 重要：原方案经实测被否定，本文件记录的是修正后的方案

参考项目 a-share-quant-selector 的 B1 匹配：**人工挑 10 只历史成功案例
→ 提取形态特征 → 找当下相似的票**（双线 30% / 量比 25% / DTW 25% / KDJ 20%）。

### 实测结论一：「像历史大涨股」没有预测力

按原思路实现后，在 300 只测试股上验证（用训练集建模板，测试集验证）：

| 相似度分组 | 样本数 | 未来20日均值 | 胜率 |
|---|---|---|---|
| 高（≥70） | 9,702 | **+1.73%** | 47.2% |
| 中（50~70） | 37,597 | +1.51% | 50.2% |
| 低（<50） | 78,477 | **+1.77%** | **51.9%** |

高相似度组与低相似度组几乎无差异（−0.04 个百分点），**胜率反而更低**。
原因是模板取自「后 20 日涨 >15%」样本的中位形态，而那个中位形态
落在**中高位（区间位置 0.62）**，本身就偏离了最优区间。

### 实测结论二：单特征在横截面上有强区分度（167,721 个观察点）

| 特征 | 最低分位胜率 | 最高分位胜率 | 幅度 |
|---|---|---|---|
| 前 60 日涨幅 | **55.6%** | **40.5%** | +2.79 pt |
| 量能比（近5/近60） | **56.6%** | **44.3%** | +2.26 pt |
| 区间位置 | **58.5%** | **46.3%** | +2.20 pt |
| 前 60 日振幅 | 53.6% | 46.8% | +0.97 pt |

**方向高度一致：越低（位置）、越缩量、前期涨得越少 → 未来越容易涨。**
这是 A 股均值回归特性的体现，也与「A 股强势形态是反向指标」的结论吻合。

### 修正方案：从「像模板」改为「按实测方向打分」

不再计算与某个模板的相似度，而是对每个特征按**实测的收益单调方向**
做分位打分，加权合成 0~100 分。分数高 = 处于实测的有利区间。

这样做的好处：
1. 有实测依据，不是照搬别人的经验权重
2. 每个维度可解释（前端能看到"你的位置分 85、量能分 30"）
3. 不依赖案例库质量（案例库的好坏已被证明难以保证）

保留 `build_case_library` / `match_market` 等函数是为了**可复现地证明
原方案无效**，以及供后续研究对比。生产用 `score_market`。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import history as H
import walkforward as wf


# ---------------------------------------------------------------------------
# 相似度权重（来源：本项目实测区分度，见模块文档）
# ---------------------------------------------------------------------------

# 权重按「区分度」归一化而来，不是照搬参考项目的 30/25/25/20。
# 区分度越高 → 越能区分「后涨」与「后跌」 → 权重越大。
DEFAULT_WEIGHTS = {
    "vol_ratio": 0.335,     # 量能比（近5日 / 近60日）—— 最强区分
    "chg60":     0.319,     # 前 60 日涨幅（负向：涨得多反而危险）
    "range60":   0.221,     # 前 60 日振幅
    "position":  0.205,     # 当前价在 60 日区间中的位置
}

# 哪些特征「越大越危险」（用于把差异统一成「距离」，越小越好）。
# chg60 与 vol_ratio 都是反向指标：大涨组的值反而更小。
# 在相似度里我们不分正反——只算「离模板多远」。正负方向已体现在
# 模板本身的取值里（模板是从大涨组采集的，天然带着低位、平量的特征）。

# 特征归一化尺度：不同特征量纲差异极大（涨幅是百分比、位置是 0~1），
# 必须各自归一化后再加权，否则量纲大的特征会主导结果。
FEATURE_SCALE = {
    "chg60":    20.0,    # 百分比，20 个百分点算一个「单位差异」
    "range60":  20.0,
    "position": 0.20,    # 0~1
    "vol_ratio": 0.25,
}


def extract_features(series: Dict[str, Any], idx: int,
                     window: int = 60) -> Optional[Dict[str, float]]:
    """提取「截止到 idx」这一天的形态特征。

    这是匹配的核心输入。所有特征都做了尺度无关处理（归一化/比值），
    因此不同价位的股票可以直接比较。
    """
    C = np.asarray(series["close"], dtype=float)
    Vh = np.asarray(series["volume"], dtype=float)
    n = len(C)
    if idx < window or idx >= n:
        return None

    seg = C[idx - window + 1: idx + 1]
    if len(seg) < window or seg[0] <= 0:
        return None
    lo, hi = float(seg.min()), float(seg.max())
    if hi <= 0 or lo <= 0:
        return None

    chg60 = (seg[-1] - seg[0]) / seg[0] * 100.0
    range60 = (hi - lo) / lo * 100.0
    position = (seg[-1] - lo) / (hi - lo) if hi > lo else 0.5

    # 量能比：近 5 日均量 / 近 60 日均量。
    # 大涨组 ≈1.05（平稳），大跌组 ≈1.25（已放量）——放量之后反而易跌。
    recent = Vh[idx - 4: idx + 1]
    base = Vh[idx - window + 1: idx + 1]
    bm = float(base.mean()) if len(base) else 0.0
    vol_ratio = (float(recent.mean()) / bm) if bm > 0 and len(recent) else 1.0

    return {
        "chg60": float(chg60),
        "range60": float(range60),
        "position": float(position),
        "vol_ratio": float(vol_ratio),
    }


def _dist(a: Dict[str, float], b: Dict[str, float],
          weights: Dict[str, float]) -> float:
    """加权归一化欧氏距离。返回 0 表示完全一致，越大越不像。"""
    total_w = 0.0
    acc = 0.0
    for k, w in weights.items():
        x, y = a.get(k), b.get(k)
        if x is None or y is None:
            continue
        scale = FEATURE_SCALE.get(k) or 1.0
        d = (x - y) / scale
        acc += w * d * d
        total_w += w
    if total_w <= 0:
        return float("inf")
    return math.sqrt(acc / total_w)


# ---------------------------------------------------------------------------
# 案例库构建
# ---------------------------------------------------------------------------

def build_case_library(codes: Sequence[str],
                       *,
                       horizon: int = 20,
                       up_threshold: float = 15.0,
                       window: int = 60,
                       max_cases: int = 200,
                       min_gap: int = 10,
                       engine: Any = None) -> List[Dict[str, Any]]:
    """从历史数据里自动采集「大涨之前」的形态，构成案例库。

    正样本定义：在 idx 这一天，其**后 horizon 日涨幅 > up_threshold%**。
    采集的是 idx 当天的形态特征（即「涨起来之前的样子」），
    而不是涨完之后的样子——这是本实现与参考项目最大的区别。

    min_gap: 同一只股票相邻案例至少间隔多少根，避免同一波行情被采成多个案例
             （它们形态几乎一样，会让案例库冗余、降低多样性）。

    返回 [{code, idx, date, fwd_ret, features}, ...]
    """
    eng = engine or H.get_engine()
    if eng is None:
        return []

    cases: List[Dict[str, Any]] = []
    for code in codes:
        s = eng.series(code)
        if not s:
            continue
        C = np.asarray(s["close"], dtype=float)
        n = len(C)
        if n < window + horizon + 5:
            continue
        last_taken = -10_000
        for idx in range(window, n - horizon):
            if idx - last_taken < min_gap:
                continue
            r = (C[idx + horizon] - C[idx]) / C[idx] * 100.0 if C[idx] > 0 else 0.0
            if r <= up_threshold:
                continue
            f = extract_features(s, idx, window)
            if not f:
                continue
            cases.append({
                "code": code,
                "idx": idx,
                "date": s["dates"][idx] if idx < len(s.get("dates") or []) else "",
                "fwd_ret": round(float(r), 2),
                "features": f,
            })
            last_taken = idx

    # 按后续涨幅降序（优先保留最强的样例），截断到上限
    cases.sort(key=lambda x: -x["fwd_ret"])
    return cases[:max_cases]


def template_from_cases(cases: Sequence[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """把案例库聚合成一个「模板特征」（各维中位数）。

    用中位数而不是均值：个别极端案例（如翻倍股）会把均值拉偏，
    中位数更能代表「典型的大涨前形态」。
    """
    if not cases:
        return None
    keys = ("chg60", "range60", "position", "vol_ratio")
    tpl: Dict[str, float] = {}
    for k in keys:
        vals = [c["features"][k] for c in cases if c["features"].get(k) is not None]
        if vals:
            tpl[k] = float(np.median(vals))
    return tpl or None


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------

def similarity_to_template(features: Dict[str, float],
                           template: Dict[str, float],
                           weights: Optional[Dict[str, float]] = None
                           ) -> Tuple[float, Dict[str, float]]:
    """算一个形态与模板的相似度（0~100，越大越像）。

    返回 (相似度, 分项得分)。分项得分用于解释「为什么像/不像」。
    """
    w = weights or DEFAULT_WEIGHTS
    parts: Dict[str, float] = {}
    total_w = 0.0
    acc = 0.0
    for k, wt in w.items():
        x, y = features.get(k), template.get(k)
        if x is None or y is None:
            continue
        scale = FEATURE_SCALE.get(k) or 1.0
        # 单维相似度：差异 0 → 100 分；差异达到 2 个 scale → 0 分
        d = abs(x - y) / scale
        s = max(0.0, 100.0 * (1.0 - d / 2.0))
        parts[k] = round(s, 1)
        acc += wt * s
        total_w += wt
    sim = (acc / total_w) if total_w > 0 else 0.0
    return round(sim, 1), parts


def match_market(rows: Sequence[Dict[str, Any]],
                 template: Dict[str, float],
                 *,
                 weights: Optional[Dict[str, float]] = None,
                 min_similarity: float = 60.0,
                 top_n: int = 50,
                 engine: Any = None,
                 window: int = 60) -> List[Dict[str, Any]]:
    """用模板匹配当前全市场（用各股最新一根的形态）。

    rows   : 行情快照（只用于拿 code 与展示字段）
    template: 由 build_case_library + template_from_cases 得到
    """
    eng = engine or H.get_engine()
    if eng is None or not template:
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        code = r.get("code")
        if not code:
            continue
        s = eng.series(code)
        if not s:
            continue
        idx = len(s["close"]) - 1
        f = extract_features(s, idx, window)
        if not f:
            continue
        sim, parts = similarity_to_template(f, template, weights)
        if sim < min_similarity:
            continue
        item = dict(r)
        item["similarity"] = sim
        item["sim_parts"] = parts
        item["feat"] = {k: round(v, 3) for k, v in f.items()}
        out.append(item)

    out.sort(key=lambda x: -x["similarity"])
    return out[:top_n]


def describe_template(template: Dict[str, float]) -> str:
    """把模板特征翻译成人话，便于前端展示「我们在找什么样的票」。"""
    if not template:
        return ""
    pos = template.get("position", 0.5)
    if pos < 0.35:
        pos_txt = "低位"
    elif pos < 0.65:
        pos_txt = "中位"
    else:
        pos_txt = "高位"
    vr = template.get("vol_ratio", 1.0)
    vol_txt = "平量" if vr < 1.15 else ("温和放量" if vr < 1.5 else "明显放量")
    return ("典型形态：前 %d 日涨幅 %+.1f%%、振幅 %.1f%%、%s（区间位置 %.2f）、%s（量能比 %.2f）"
            % (60, template.get("chg60", 0.0), template.get("range60", 0.0),
               pos_txt, pos, vol_txt, vr))


# ===========================================================================
# 修正方案：按实测方向打分（生产使用）
# ===========================================================================
#
# 上面那套「与模板比相似度」经实测无预测力（见模块文档）。这里改用第二种
# 做法：对每个特征按**实测的收益单调方向**做分位打分，再加权合成。
#
# 分位阈值来自 167,721 个观察点的实测（20 日收益）：
#
#   特征          方向        有利区间            不利区间
#   ────────────────────────────────────────────────────────
#   区间位置      越低越好    0 ~ 0.3（胜率58.5%）  > 0.7（46.3%）
#   量能比        越低越好    < 0.95（56.6%）      > 1.3（44.3%）
#   前60日涨幅    越低越好    < 0%（55.6%）        > 25%（40.5%）
#   前60日振幅    越低越好    小振幅更优           （区分度较弱）
#
# 每个特征用「分段线性」映射到 0~100 分：落在有利区间给高分，
# 落在不利区间给低分，中间线性过渡。

# 特征权重：按实测区分幅度（胜率极差）归一化。
#   前60日涨幅 2.79pt / 量能比 2.26pt / 区间位置 2.20pt / 振幅 0.97pt
# 归一化后（总权重 = 1）：
SCORE_WEIGHTS = {
    "chg60":    0.34,
    "vol_ratio": 0.28,
    "position": 0.27,
    "range60":  0.11,
}

# 每个特征的评分控制点：(值, 得分)。分段线性插值。
# 值越小越好，所以控制点按值升序、得分降序排列。
_SCORE_CURVES = {
    # 区间位置 0~1：低位最优
    "position":  [(0.00, 100), (0.20, 95), (0.35, 78), (0.50, 60),
                  (0.65, 40), (0.80, 22), (1.00, 10)],
    # 量能比：缩量/平量最优（放量反而危险）
    "vol_ratio": [(0.60, 100), (0.85, 92), (1.00, 78), (1.15, 60),
                  (1.30, 40), (1.60, 20), (2.20, 8)],
    # 前 60 日涨幅（%）：前面涨得少更优
    "chg60":     [(-30, 100), (-10, 96), (0, 88), (10, 70),
                  (25, 45), (40, 25), (80, 8)],
    # 前 60 日振幅（%）：小振幅略优（区分度弱，故权重低）
    "range60":   [(10, 95), (25, 85), (40, 68), (55, 52),
                  (75, 36), (120, 18)],
}


def _curve_score(value: float, curve: Sequence[Tuple[float, float]]) -> float:
    """在分段线性曲线上插值出得分（0~100）。超界按端点取值。"""
    if value <= curve[0][0]:
        return float(curve[0][1])
    if value >= curve[-1][0]:
        return float(curve[-1][1])
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return float(y1)
            t = (value - x0) / (x1 - x0)
            return float(y0 + (y1 - y0) * t)
    return 50.0


def score_features(features: Dict[str, float],
                   weights: Optional[Dict[str, float]] = None
                   ) -> Tuple[float, Dict[str, float]]:
    """按实测方向给一个形态打分（0~100，越高越处于有利区间）。

    返回 (总分, 分项得分)。分项便于前端解释「为什么高分/低分」。
    """
    w = weights or SCORE_WEIGHTS
    parts: Dict[str, float] = {}
    total_w = 0.0
    acc = 0.0
    for k, wt in w.items():
        v = features.get(k)
        curve = _SCORE_CURVES.get(k)
        if v is None or not curve:
            continue
        s = _curve_score(float(v), curve)
        parts[k] = round(s, 1)
        acc += wt * s
        total_w += wt
    score = (acc / total_w) if total_w > 0 else 0.0
    return round(score, 1), parts


def score_market(rows: Sequence[Dict[str, Any]],
                 *,
                 weights: Optional[Dict[str, float]] = None,
                 min_score: float = 70.0,
                 top_n: int = 50,
                 engine: Any = None,
                 window: int = 60) -> List[Dict[str, Any]]:
    """给当前全市场按「实测有利形态」打分并排序。

    这是 P1 的生产入口。分数含义：
      >= 80  形态处于实测最优区间（低位 + 缩量 + 前期滞涨）
      70~80  较优
      < 70   不满足任一门槛（默认过滤掉）
    """
    eng = engine or H.get_engine()
    if eng is None:
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        code = r.get("code")
        if not code:
            continue
        s = eng.series(code)
        if not s:
            continue
        idx = len(s["close"]) - 1
        f = extract_features(s, idx, window)
        if not f:
            continue
        sc, parts = score_features(f, weights)
        if sc < min_score:
            continue
        item = dict(r)
        item["score"] = sc
        item["score_parts"] = parts
        item["feat"] = {k: round(v, 3) for k, v in f.items()}
        out.append(item)

    out.sort(key=lambda x: -x["score"])
    return out[:top_n]


def score_series_at(series: Dict[str, Any], idx: int,
                    weights: Optional[Dict[str, float]] = None,
                    window: int = 60) -> Optional[Tuple[float, Dict[str, float]]]:
    """对单只股票某个时点打分（供回测/研究用）。"""
    f = extract_features(series, idx, window)
    if not f:
        return None
    return score_features(f, weights)
