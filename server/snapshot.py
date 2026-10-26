"""数据快照冻结 + SHA-256 —— 让"上次算的 IC 和这次不一样"变成可查证的事。

## 为什么需要它

因子/回溯这类结果有两个常见争议：
  · "昨天 IC 还是 0.08，今天怎么变 0.03 了？"——是数据变了，还是代码变了，
    还是本来就该变（新的一天入库）？现在只能猜。
  · "这个截图是你那时候的界面吗？"——没有任何东西能把一张图钉回当时的数据。

做法与 TradingView-API 的 `freezeSnapshot` 同构：**把喂给计算的数据本身
规范化并哈希**，让"输入完全相同 ⇒ 哈希相同 ⇒ 结果本该相同"成为可检验的断言。

## 冻结时做的四件事（顺序有意义）

1. **丢弃未完成 bar**：最后一根如果日期是今天且行情还没收盘，它每天在变，
   留在快照里会让同一天算两次得到两个哈希。判定用 `is_settled`，
   而不是简单"删掉最后一行"——后者在盘后会把好数据也删掉。
2. **OHLC 一致性校验**：`high >= max(open, close)`、`low <= min(open, close)`、
   所有价格 > 0。坏一行就整份快照不可信，所以**直接拒绝**并列出行号，
   不做静默修补（修补等于把脏数据洗白，哈希就失去意义）。
3. **日期严格递增 + 重复检测**：日期乱序或重复会导致"同一份数据、不同哈希"，
   也让前视检查失效。
4. **缺口检测**：相邻交易日跨度超过自然日阈值就记为 gap。不判断"该不该有"
   （节假日由 `holidays.py` 管），只如实记录——快照的职责是描述，不是修正。

## 哈希口径（必须写死，否则跨版本不可复现）

对 `code|date|open|close|high|low|volume|amount` 逐行、**定长格式化**后拼接再哈希：

- 价格/数量统一 `%.6f`，`None`/NaN 写成 `nan`。
  用 `repr(float)` 不行：`1.0` 与 `1` 打印相同但二进制可能不同；
  用 `%.6f` 是我们能接受的最小精度损失，且跨机器稳定。
- 行按 `(code, date)` **排序**后拼接，与查询返回顺序无关。
  否则 SQL 的 `ORDER BY` 一变，哈希就变。
- 行间用 `\n`，字段间用 `|`。日期是 `YYYY-MM-DD`，代码是 `sh600519` 这种
  纯 ASCII，不含分隔符，因此拼接无歧义。

## 与 A2（因果性检查）的配对

快照记录"算的是什么数据"，因果性检查记录"算的时候有没有偷看未来"。
两者合起来才能说：**同一份数据 + 同样的因果约束 ⇒ 同一个结论**。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# 快照版本：哈希口径一旦变化必须加一，否则旧报告会被误判"可复现"
SNAPSHOT_VERSION = 1

PRICE_KEYS = ("open", "close", "high", "low")
MAT_KEYS = ("open", "close", "high", "low", "volume", "amount")

# 相邻交易日跨度超过这么多自然日就记一个 gap（含周末：周五→周一 = 3 天）
GAP_DAYS = 4


def _f(x: Any) -> str:
    """定长浮点格式化；None/NaN/inf 一律 'nan'。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "nan"
    if not math.isfinite(v):
        return "nan"
    return f"{v:.6f}"


def is_settled(last_date: str, today: str, now_hm: Optional[str] = None,
               close_hm: str = "15:30") -> bool:
    """最后一根 bar 是否已经"定稿"（可以进快照）。

    A 股 15:00 收盘，但落库通常滞后到 15:30 之后。所以：
      · 最后一根日期早于今天 → 已定稿（无论现在几点）
      · 最后一根日期就是今天 → 只有过了 close_hm 才算定稿
    盘前/盘中把今天那根半成品放进快照，会让**同一个交易日内**每跑一次
    都得到不同哈希，快照就退化成"时间戳"了。
    """
    if not last_date:
        return False
    if last_date < today:
        return True
    if last_date > today:
        return True          # 库里出现未来日期属于异常，但不算"未定稿"，交给校验报
    if not now_hm:
        return False
    return now_hm >= close_hm


def freeze(mats: Dict[str, Any],
           codes: Optional[Sequence[str]] = None,
           today: str = "",
           now_hm: Optional[str] = None,
           drop_unsettled: bool = True,
           strict: bool = True) -> Dict[str, Any]:
    """把矩阵冻结成可哈希的快照描述。

    参数：
        mats:   {dates, codes, open, close, high, low, volume, amount}
                —— 与 `factor_lab.load_universe` 返回同构。
        today:  "YYYY-MM-DD"，用于判定最后一根是否定稿；空则不做定稿裁剪。
        strict: True 时任何校验失败都返回 ok=False 且不产出哈希。

    返回：
        {
          "ok": bool, "version": 1, "sha256": str|None,
          "rows": int, "codes": int, "days": int,
          "span": [from, to],
          "dropped": {"unsettled": n, "all_nan_rows": n},
          "gaps": [{from, to, calendar_days}],
          "problems": [...],           # 校验失败项（ok=False 时非空）
          "fingerprint": {...},        # 便于人眼核对的分项摘要
        }
    """
    dates: List[str] = [str(d) for d in (mats.get("dates") or [])]
    cs: List[str] = [str(c) for c in (codes if codes is not None
                                      else (mats.get("codes") or []))]
    T, N = len(dates), len(cs)
    problems: List[Dict[str, Any]] = []

    arrs = {k: mats.get(k) for k in MAT_KEYS}
    for k, a in arrs.items():
        if not isinstance(a, np.ndarray) or a.ndim != 2:
            problems.append({"kind": "missing_matrix", "field": k})
        elif a.shape != (T, N):
            problems.append({"kind": "shape", "field": k,
                             "got": list(a.shape), "want": [T, N]})
    if problems:
        return {"ok": False, "version": SNAPSHOT_VERSION, "sha256": None,
                "rows": 0, "codes": N, "days": T, "span": ["", ""],
                "dropped": {"unsettled": 0, "all_nan_rows": 0},
                "gaps": [], "problems": problems, "fingerprint": {}}

    # ── 1. 未定稿 bar
    dropped_unsettled = 0
    keep_T = T
    if drop_unsettled and dates and today:
        last = dates[-1]
        if not is_settled(last, today, now_hm) and last == today:
            keep_T = T - 1
            dropped_unsettled = 1
            dates = dates[:keep_T]
            arrs = {k: v[:keep_T] for k, v in arrs.items()}

    # ── 2. 日期严格递增 + 重复
    for i in range(1, len(dates)):
        if dates[i] == dates[i - 1]:
            problems.append({"kind": "duplicate_date", "date": dates[i]})
            break
        if dates[i] < dates[i - 1]:
            problems.append({"kind": "date_order", "at": i,
                             "prev": dates[i - 1], "cur": dates[i]})
            break

    # ── 3. OHLC 一致性（逐行逐列，报出前 5 个）
    o, c, h, l, v, a = (arrs[k] for k in MAT_KEYS)
    with np.errstate(invalid="ignore"):
        bad_hi = np.isfinite(c) & np.isfinite(h) & (h < c - 1e-9)
        bad_hi |= np.isfinite(o) & np.isfinite(h) & (h < o - 1e-9)
        bad_lo = np.isfinite(c) & np.isfinite(l) & (l > c + 1e-9)
        bad_lo |= np.isfinite(o) & np.isfinite(l) & (l > o + 1e-9)
        bad_px = np.zeros_like(bad_hi)
        for m in (o, c, h, l):
            bad_px |= np.isfinite(m) & (m <= 0)
    for name, mask in (("high_lt_body", bad_hi), ("low_gt_body", bad_lo),
                       ("non_positive_price", bad_px)):
        idx = np.argwhere(mask)
        for t, j in idx[:5]:
            problems.append({"kind": name,
                             "date": dates[int(t)] if t < len(dates) else "?",
                             "code": cs[int(j)] if j < len(cs) else "?",
                             "open": _f(o[t, j]), "close": _f(c[t, j]),
                             "high": _f(h[t, j]), "low": _f(l[t, j])})
        if len(idx) > 5:
            problems.append({"kind": name, "more": int(len(idx) - 5)})

    # ── 4. 缺口（只记录，不判对错）
    gaps: List[Dict[str, Any]] = []
    import datetime as _dt
    for i in range(1, len(dates)):
        try:
            d0 = _dt.date.fromisoformat(dates[i - 1])
            d1 = _dt.date.fromisoformat(dates[i])
        except ValueError:
            problems.append({"kind": "bad_date_format", "date": dates[i]})
            break
        span = (d1 - d0).days
        if span > GAP_DAYS:
            gaps.append({"from": dates[i - 1], "to": dates[i],
                         "calendar_days": span})

    # ── 5. 哈希：只对"整行至少有一个有限值"的行计入（全 NaN 行是停牌占位）
    lines: List[str] = []
    rows = 0
    all_nan_rows = 0
    for t in range(len(dates)):
        any_ok = any(np.isfinite(arrs[k][t, :]).any() for k in MAT_KEYS)
        if not any_ok:
            all_nan_rows += 1
            continue
        for j in range(N):
            # 整只股票该行全空则不占行，避免"停牌占位"影响哈希
            if not any(np.isfinite(arrs[k][t, j]) for k in MAT_KEYS):
                continue
            rows += 1
            lines.append("|".join([cs[j], dates[t]] +
                                  [_f(arrs[k][t, j]) for k in MAT_KEYS]))
    lines.sort()
    h256 = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    fp = {
        "version": SNAPSHOT_VERSION,
        "fields": list(MAT_KEYS),
        "rows": rows,
        "codes": N,
        "days": len(dates),
        "span": [dates[0], dates[-1]] if dates else ["", ""],
        "last_close_sum": _f(np.nansum(c[:, -1])) if len(dates) and N else "nan",
    }
    return {
        "ok": not problems or not strict,
        "version": SNAPSHOT_VERSION,
        "sha256": h256 if (not problems or not strict) else None,
        "rows": rows, "codes": N, "days": len(dates),
        "span": fp["span"],
        "dropped": {"unsettled": dropped_unsettled, "all_nan_rows": all_nan_rows},
        "gaps": gaps,
        "problems": problems,
        "fingerprint": fp,
    }


def verify(snapshot: Dict[str, Any], mats: Dict[str, Any],
           codes: Optional[Sequence[str]] = None,
           today: str = "", now_hm: Optional[str] = None) -> Dict[str, Any]:
    """按给定数据重算快照哈希，比对是否与记录一致。

    这是"能不能复现"的唯一判据：**只比哈希，不猜原因**。
    不一致时把两边的分项摘要并排放出来（行数/跨度），让用户自己看出是
    数据追加、还是数据被修正过。
    """
    old = (snapshot or {}).get("sha256")
    fresh = freeze(mats, codes=codes, today=today, now_hm=now_hm, strict=False)
    if not old:
        return {"ok": False, "reason": "快照没有哈希，无法校验",
                "now": fresh.get("sha256")}
    same = old == fresh.get("sha256")
    return {
        "ok": same,
        "reason": "与快照一致，可用同一份数据复现" if same else
                  "数据已变化：哈希不一致（可能是新数据入库，或历史被修正）",
        "recorded": old,
        "now": fresh.get("sha256"),
        "recorded_fp": (snapshot or {}).get("fingerprint") or {},
        "now_fp": fresh.get("fingerprint") or {},
    }
