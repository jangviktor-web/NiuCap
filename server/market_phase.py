"""市场情绪周期（6 阶段）与主线识别 —— 牛来选股面板 #95。

来源：蒸馏报告 docs/蒸馏报告_shy3130-tick-stock-panel.md 第 8.1/8.2/8.3 节
（shy3130/tick-stock-panel 的 market_phase.py + market_mainline.py，MIT）。

设计要点：
  - 连板数由本地 daily_bars 的 close / prev_close 推导（prev_close = LAG(close)），
    零新增采集；板块阈值复用 newbie._board 口径（sz300=创业板20% / sh688=科创板20%
    / bj=北交所30% / 其余主板10%），ST 不计连板（合理近似）。
  - 6 阶段阈值常量原样照搬对方 2020-08~2026-08 标定值（我们不重标定）。
  - 判定顺序严格按源码注释：高潮 > 主升 > 冰点(优先于退潮) > 退潮 > 启动 > 修复；
    EMA(alpha=1/3) 平滑 + 连续 2 日确认防抖动。
  - 弱档否决（state 列）我方无 5 档 state，首版跳过（注释保留说明）。
  - 全序列一次性计算后缓存 + JSON 落盘，按 daily_bars 最大日期判脏。
  - 主线分两路：实时主线用同花顺 limit_up_pool 按 reason 聚合（我方无历史概念映射表）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ponytail: 只扫最近 N 自然日的日线（约 340 交易日）。阶段判定只看最近 ~20 交易日，
# 连板数在窗口首日缺 prev_close 会偏小，但 3~5 天后即准确，不影响判定与展示。
# 全表无 WHERE 的 ORDER BY code,date 会触发 2.7M 行外排序（慢磁盘上 >8min），
# 带 date 过滤则走索引顺序扫描（实测 978k 行 0.6s）。窗口不够用时调大此值。
_WINDOW_DAYS = 500

# ───────────────────────── 阶段词汇 ─────────────────────────
PHASE_ICE = "ice"
PHASE_IGNITE = "ignite"
PHASE_RALLY = "rally"
PHASE_CLIMAX = "climax"
PHASE_EBB = "ebb"
PHASE_REPAIR = "repair"

PHASE_LABELS = {
    PHASE_ICE: "冰点",
    PHASE_IGNITE: "启动",
    PHASE_RALLY: "主升",
    PHASE_CLIMAX: "高潮",
    PHASE_EBB: "退潮",
    PHASE_REPAIR: "修复",
}
# 阶段配色（避开涨跌红绿）：冰点蓝 / 启动绿 / 主升红 / 高潮深红 / 退潮橙 / 修复灰
PHASE_COLORS = {
    PHASE_ICE: "#3b82f6",
    PHASE_IGNITE: "#22c55e",
    PHASE_RALLY: "#ef4444",
    PHASE_CLIMAX: "#b91c1c",
    PHASE_EBB: "#f59e0b",
    PHASE_REPAIR: "#94a3b8",
}

# ───────────────────────── 阈值（照搬对方标定常量） ─────────────────────────
CLIMAX_GE2 = 50
CLIMAX_FIRST_BOARD = 220
RALLY_HEIGHT = 7
RALLY_GE2 = 15
RALLY_PROMO = 0.23
RALLY_PROMO_ALT = 0.30
RALLY_GE2_ALT = 12
RALLY_HEIGHT_ALT = 5
EBB_PROMO = 0.15
EBB_PROMO_STRICT = 0.13
EBB_SEAL = 0.57
EBB_RECENT_GE2 = 12
EBB_RECENT_HEIGHT = 6
IGNITE_GE2_DELTA = 3
IGNITE_GE2 = 8
IGNITE_PROMO = 0.20
IGNITE_HEIGHT_DELTA = 1
IGNITE_HEIGHT = 5
IGNITE_PROMO_SOFT = 0.19
ICE_HEIGHT = 4
ICE_GE2 = 6
ICE_FIRST_BOARD = 24
PROMO_MIN_POOL = 10
_EMA_ALPHA = 1.0 / 3.0
_CONFIRM_DAYS = 2

# 主线分权重（照搬 8.2）
_SCORE_WEIGHTS = {
    "count": 0.35,
    "max_boards": 0.25,
    "rungs_filled": 0.25,
    "ge2": 0.15,
}
_MIN_LIMIT_UP = 3
_TOP_MAINLINE = 12

_CACHE_PATH_DEFAULT = "data/market_phase_daily.json"
_lock = threading.Lock()
_CACHE: dict | None = None  # {"max_date": str, "rows": [ {...} ]}


# ───────────────────────── 板块阈值 ─────────────────────────
def board_pct(code: str) -> float:
    """按代码前缀返回涨跌停幅度（%），与 newbie._board 口径一致。"""
    if code.startswith("bj"):
        return 30.0
    d = code[2:5] if len(code) >= 5 else code
    if d[:2] == "30":  # 创业板 300/301/302...
        return 20.0
    if d[:3] in ("688", "689"):  # 科创板
        return 20.0
    return 10.0


# ───────────────────────── 连板数推导 + 日频聚合 ─────────────────────────
def compute_daily_series(conn, window_days: int = _WINDOW_DAYS) -> list[dict]:
    """从 daily_bars 计算每日连板梯队聚合（最近 window_days 自然日）。按 date 升序。

    流式单遍扫描：按 code,date 排序，逐行维护每只股票状态（prev_close / consec），
    边扫边累加到按 date 的聚合字典。O(n) 无窗口函数，内存恒定。
    """
    t0 = time.time()
    maxd = conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
    if not maxd:
        return []
    start = (datetime.strptime(str(maxd)[:10], "%Y-%m-%d")
             - timedelta(days=window_days)).strftime("%Y-%m-%d")
    cur = conn.execute(
        "SELECT code, date, close, high FROM daily_bars "
        "WHERE date >= ? ORDER BY code, date", (start,)
    )
    # code -> (prev_close, consec)；pct 缓存避免 2.7M 次字符串判定
    state: dict = {}
    pct_cache: dict = {}
    daily: dict = {}
    for code, date, close, high in cur:
        pc, consec = state.get(code, (None, 0))
        pct = pct_cache.get(code)
        if pct is None:
            pct = pct_cache[code] = board_pct(code)
        is_lu = bool(pc and close >= pc * (1 + pct / 100.0 - 0.005))
        touched = bool(pc and high >= pc * (1 + pct / 100.0 - 0.005))
        new_consec = consec + 1 if is_lu else 0
        d = daily.get(date)
        if d is None:
            d = daily[date] = [0, 0, 0, 0, 0, 0, 0, 0, 0]  # h,f,ge2,ge3,ge5,sealed,broken,pool,ok
        if new_consec > d[0]:
            d[0] = new_consec
        if new_consec == 1:
            d[1] += 1
        if new_consec >= 2:
            d[2] += 1
        if new_consec >= 3:
            d[3] += 1
        if new_consec >= 5:
            d[4] += 1
        if is_lu:
            d[5] += 1
        if touched and not is_lu:
            d[6] += 1
        if consec >= 1:
            d[7] += 1
        if consec >= 1 and new_consec == consec + 1:
            d[8] += 1
        state[code] = (close, new_consec)

    rows = []
    for date in sorted(daily.keys()):
        h, f, ge2, ge3, ge5, sealed, broken, pool, ok = daily[date]
        promo = round(ok / pool, 4) if pool >= PROMO_MIN_POOL else None
        seal = round(sealed / (sealed + broken), 4) if (sealed + broken) > 0 else None
        rows.append({
            "date": date,
            "height": int(h),
            "first_board": int(f),
            "ge2": int(ge2),
            "ge3": int(ge3),
            "ge5": int(ge5),
            "promo_rate": promo,
            "seal_rate": seal,
        })
    logger.info("compute_daily_series: %d 交易日, 耗时 %.2fs", len(rows), time.time() - t0)
    return rows


# ───────────────────────── EMA + 阶段判定 ─────────────────────────
def _ema(values: list[float | None], alpha: float = _EMA_ALPHA) -> list[float | None]:
    out = []
    cur = None
    for v in values:
        if v is None or (isinstance(v, float) and v != v):
            out.append(cur)
            continue
        cur = v if cur is None else cur + alpha * (v - cur)
        out.append(cur)
    fv = next((i for i, x in enumerate(out) if x is not None), None)
    if fv is not None:
        for i in range(fv):
            out[i] = out[fv]
    elif not out:
        out = [0.0] * len(values)
    else:
        out = [0.0] * len(values)
    return out


def _raw_label(i, h, fb, g2, pr, sr, g2_prev, h_prev) -> str:
    if g2 >= CLIMAX_GE2 or fb >= CLIMAX_FIRST_BOARD:
        return PHASE_CLIMAX
    if h >= RALLY_HEIGHT and g2 >= RALLY_GE2 and (pr or 0) >= RALLY_PROMO:
        return PHASE_RALLY
    if (pr or 0) >= RALLY_PROMO_ALT and g2 >= RALLY_GE2_ALT and h >= RALLY_HEIGHT_ALT:
        return PHASE_RALLY
    # 冰点优先于退潮（长期死寂 ≠ 自高位退潮）
    if h <= ICE_HEIGHT and g2 <= ICE_GE2 and fb <= ICE_FIRST_BOARD:
        return PHASE_ICE
    from_high = g2_prev >= EBB_RECENT_GE2 or h_prev >= EBB_RECENT_HEIGHT
    if from_high and ((pr or 1) <= EBB_PROMO and g2 < g2_prev):
        return PHASE_EBB
    if (pr or 1) <= EBB_PROMO_STRICT and (sr if sr is not None else 1) <= EBB_SEAL:
        return PHASE_EBB
    if g2 - g2_prev >= IGNITE_GE2_DELTA and g2 >= IGNITE_GE2 and (pr or 0) >= IGNITE_PROMO:
        return PHASE_IGNITE
    if (h - h_prev) >= IGNITE_HEIGHT_DELTA and h >= IGNITE_HEIGHT and (pr or 0) >= IGNITE_PROMO_SOFT:
        return PHASE_IGNITE
    return PHASE_REPAIR


def classify_phase_series(rows: list[dict]) -> list[dict]:
    """对完整日序打阶段标签（含 EMA 平滑 + 连续 2 日确认）。返回附带 phase 的行。"""
    n = len(rows)
    if n == 0:
        return rows
    h_s = _ema([float(r["height"]) for r in rows])
    fb_s = _ema([float(r["first_board"]) for r in rows])
    g2_s = _ema([float(r["ge2"]) for r in rows])
    pr_s = _ema([(r["promo_rate"] if r["promo_rate"] is not None else None) for r in rows])
    sr_s = _ema([(r["seal_rate"] if r["seal_rate"] is not None else None) for r in rows])

    labels = []
    current = None
    pending = None
    pending_run = 0
    for i in range(n):
        g2_prev = g2_s[max(0, i - 5)]
        h_prev = h_s[max(0, i - 5)]
        raw = _raw_label(i, h_s[i], fb_s[i], g2_s[i], pr_s[i], sr_s[i], g2_prev, h_prev)
        if current is None:
            current = raw
            labels.append(raw)
            continue
        if raw == current:
            labels.append(current)
            pending, pending_run = None, 0
            continue
        if raw == pending:
            pending_run += 1
        else:
            pending, pending_run = raw, 1
        if pending_run >= _CONFIRM_DAYS:
            current = raw
            labels.append(current)
            pending, pending_run = None, 0
        else:
            labels.append(current)
    for r, ph in zip(rows, labels):
        r["phase"] = ph
    return rows


# ───────────────────────── 缓存（判脏 + JSON 落盘） ─────────────────────────
def _db_max_date(conn) -> str:
    return conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0] or ""


def _load_cache_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache_file(path: str, cache: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("market_phase cache save failed: %s", e)


def _build(conn, cache_path: str) -> dict:
    rows = compute_daily_series(conn)
    classify_phase_series(rows)
    cache = {"max_date": _db_max_date(conn), "rows": rows}
    _save_cache_file(cache_path, cache)
    return cache


def ensure_ready(conn, cache_path: str = _CACHE_PATH_DEFAULT, force: bool = False) -> dict:
    """返回 {max_date, rows(含 phase)}；按 daily_bars 最大日期判脏，脏则重建。"""
    global _CACHE
    with _lock:
        if _CACHE and not force:
            if _CACHE.get("max_date") == _db_max_date(conn):
                return _CACHE
        cached = _load_cache_file(cache_path)
        if cached and not force and cached.get("max_date") == _db_max_date(conn):
            _CACHE = cached
            return _CACHE
        _CACHE = _build(conn, cache_path)
        return _CACHE


def _abs_cache_path(path: str = _CACHE_PATH_DEFAULT) -> str:
    """默认常量是相对路径 'data/...'，取决于启动 cwd，纳管前必须归一成绝对路径。"""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)


def cache_status(path: str = _CACHE_PATH_DEFAULT) -> dict:
    """缓存占用情况，供后台「缓存统一管理」展示（#97）。

    内存缓存可能为空（进程刚起还没算过），此时只报磁盘 JSON 的体量。
    """
    p = _abs_cache_path(path)
    b = os.path.getsize(p) if os.path.isfile(p) else 0
    c = _CACHE or {}
    rows = c.get("rows") or []
    ph = None
    if rows:
        ph = rows[-1].get("phase")
        ph = PHASE_LABELS.get(ph, ph)
    return {
        "ready": bool(rows),
        "in_memory": _CACHE is not None,
        "max_date": c.get("max_date") or "",
        "rows": len(rows),
        "phase": ph,
        "path": p,
        "bytes": b,
        "mtime": (datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")
                  if b else None),
    }


def clear_cache(path: str = _CACHE_PATH_DEFAULT) -> dict:
    """清掉内存 + 磁盘缓存。下次访问会重算——窗口剪枝后约 2 秒，不阻塞。"""
    global _CACHE
    with _lock:
        _CACHE = None
    p = _abs_cache_path(path)
    had = os.path.isfile(p)
    try:
        if had:
            os.remove(p)
    except Exception as e:
        return {"ok": False, "cleared": had, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "cleared": had}


# ───────────────────────── 对外聚合 ─────────────────────────
def get_phase_data(conn, cache_path: str = _CACHE_PATH_DEFAULT,
                   history_days: int = 20) -> dict:
    """市场情绪周期 + 主线，供 /api/market_phase 使用。"""
    rows = ensure_ready(conn, cache_path).get("rows") or []
    if not rows:
        return {"ready": False, "reason": "no_data"}
    last = rows[-1]
    hist = rows[-history_days:] if history_days else rows
    ladder = {
        "height": last["height"],
        "first_board": last["first_board"],
        "ge2": last["ge2"],
        "ge3": last["ge3"],
        "ge5": last["ge5"],
        "promo_rate": last["promo_rate"],
        "seal_rate": last["seal_rate"],
    }
    return {
        "ready": True,
        "phase": last["phase"],
        "label": PHASE_LABELS.get(last["phase"], last["phase"]),
        "color": PHASE_COLORS.get(last["phase"], "#94a3b8"),
        "as_of": last["date"],
        "ladder": ladder,
        "history": [
            {"date": r["date"], "phase": r["phase"],
             "label": PHASE_LABELS.get(r["phase"], r["phase"]),
             "color": PHASE_COLORS.get(r["phase"], "#94a3b8")}
            for r in hist
        ],
    }


def get_mainline(htk, top: int = _TOP_MAINLINE) -> dict:
    """实时主线：同花顺涨停池按 reason 聚合（我方无历史概念映射表）。"""
    try:
        pool = htk.limit_up_pool()
    except Exception as e:
        logger.warning("mainline limit_up_pool failed: %s", e)
        return {"source": "unavailable", "items": []}
    items = pool.get("items") or []
    if not items:
        return {"source": "hithink", "available": pool.get("total", 0) > 0, "items": []}
    recs: dict = {}
    for it in items:
        cnt = int(it.get("continue_day_cnt") or 1)
        # 同花顺 reason 是「概念A+概念B+...」长文本，整串聚合会得到 1 只票的伪主线；
        # 拆成概念标签再聚合，才是真板块主线（一只票可同时贡献多个概念）。
        tags = [t.strip() for t in re.split(r"[+＋、/]", it.get("reason") or "") if t.strip()]
        if not tags:
            tags = ["其他"]
        for tag in tags:
            r = recs.setdefault(tag, {"reason": tag, "count": 0, "max_boards": 0,
                                      "rungs": set(), "ge2": 0, "leaders": []})
            r["count"] += 1
            r["max_boards"] = max(r["max_boards"], cnt)
            if cnt >= 2:
                r["ge2"] += 1
                r["rungs"].add(cnt)
            r["leaders"].append({
                "name": it.get("name", ""),
                "code": it.get("code", ""),
                "boards": cnt,
                "change_pct": it.get("change_pct"),
            })
    for r in recs.values():
        r["rungs_filled"] = len(r["rungs"])
        r["leaders"] = sorted(r["leaders"], key=lambda x: -x["boards"])[:3]
        del r["rungs"]
    rec_list = list(recs.values())
    # 截面 rank 归一 → 加权主线分
    if len(rec_list) >= 2:
        for col in _SCORE_WEIGHTS:
            vals = [r[col] for r in rec_list]
            mx = max(vals) or 1
            for r in rec_list:
                r["_" + col + "_r"] = (r[col] / mx) if mx > 0 else 0.0
        for r in rec_list:
            r["score"] = round(100 * sum(_SCORE_WEIGHTS[c] * r["_" + c + "_r"]
                                         for c in _SCORE_WEIGHTS), 1)
            for c in _SCORE_WEIGHTS:
                del r["_" + c + "_r"]
    else:
        for r in rec_list:
            r["score"] = round(100 * sum(_SCORE_WEIGHTS[c] * (1 if r[c] > 0 else 0)
                                         for c in _SCORE_WEIGHTS), 1)
    rec_list.sort(key=lambda x: -x["score"])
    return {"source": "hithink", "available": True, "items": rec_list[:top]}
