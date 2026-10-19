"""牛熊温度评分（0-100）+ 六档仓位映射。

为什么要有：市场温度散落在 13 个维度里，单看「涨停家数」或单看「指数涨跌」
都容易以偏概全——前者是情绪单点，后者是单一标的。本模块只做一件事：把上游
westock 的 13 维按「估值 / 资金 / 情绪 / 技术」四类归并加权，压成一个 0-100
分，再落到六档仓位建议。

数据流：
  score(ov)  ← westock.market_overview() 的返回（含 dims 13 维，各维 1~5 分）
             → 四类归并（类内等权）→ 类间加权 → 0-100 总分 → band() 查六档

设计要点：
  - **分数越高 = 市场越热 = 建议仓位越低**。「热」不是好事，是风险累积，
    所以 90+ 是「疯狂区」配 0~10% 仓位，0~20 才是「冰点区」配 60~80%。
    这是反直觉的，UI 必须说清，否则用户会按字面把高分当成买入信号。
  - **缺失维度做权重再分配**，不做「缺了就按 50 分算」。若某类一个维度都取不到，
    把它的权重按剩余类的权重比例摊掉，总分仍是 0-100 可比的；
    直接补 50 分会把「没取到数」伪装成「市场不冷不热」，那是把故障显示成结论。
  - **不新增外部数据源**。原方案要的两融余额 / 10Y 国债 ERP / 破净股数，
    实测都无稳定免费源（北向自 2024-08 起交易所已停止实时披露，接了也是半残数据），
    全部跳过：资金维目前只有「成交量能」一个维度撑着，在 coverage 里如实标注，
    不假装资金面已被完整刻画。要补强资金维，先解决源，再谈权重。

ponytail: 权重（估值30/资金25/情绪25/技术20）沿用外部模板的给定值，未做本地
拟合——没有历史区间数据去证伪它，本地调参只会把主观意见伪装成统计量。当前
资金维只有一个维度却占 25%，是这套权重最脆的地方：一旦成交量能缺失，资金维
整体退出后权重会全摊给其余三类，分数的构成就变了。要看懂分数，看 groups 明细
而不是只看总分。
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 13 维 → 四类的归并与类间权重。类内等权。
_GROUPS: Tuple[Tuple[str, Tuple[str, ...], float], ...] = (
    ("估值", ("估值水平",), 0.30),
    ("资金", ("成交量能",), 0.25),
    ("情绪", ("情绪指标", "个股宽度", "板块宽度"), 0.25),
    ("技术", ("技术指标", "中长期趋势方向", "中长期趋势强度",
              "短期趋势方向", "短期趋势强度", "行业轮动", "风格轮动",
              "大小盘轮动"), 0.20),
)

_ALL_DIMS = tuple(n for _, names, _ in _GROUPS for n in names)
_RAW_MIN, _RAW_MAX = 1.0, 5.0  # 上游各维原始量纲

# 六档：分数区间 → 名称 / 建议仓位 / 信号灯色 / 一句话定性
_BANDS = (
    (0, 20, "冰点区", 0.60, 0.80, "green", "多数人不敢买，通常是配置窗口"),
    (20, 40, "低迷区", 0.40, 0.60, "lime", "情绪未起，适合分批慢慢建"),
    (40, 60, "震荡区", 0.30, 0.50, "yellow", "上有顶下有底，重个股轻仓位"),
    (60, 80, "活跃区", 0.20, 0.40, "orange", "赚钱效应扩散，别追高"),
    (80, 90, "过热区", 0.10, 0.20, "red", "情绪高涨，逐步降低仓位"),
    (90, 101, "疯狂区", 0.00, 0.10, "crimson", "人人谈股，风险优先于收益"),
)


def _norm(raw: float) -> float:
    """上游 1~5 原始分 → 0~100。越界截断，不抛异常（上游换量纲时不会整页挂掉）。"""
    if raw < _RAW_MIN:
        raw = _RAW_MIN
    if raw > _RAW_MAX:
        raw = _RAW_MAX
    return (raw - _RAW_MIN) / (_RAW_MAX - _RAW_MIN) * 100.0


def band_of(score: float) -> Dict[str, Any]:
    for lo, hi, name, plo, phi, light, note in _BANDS:
        if lo <= score < hi:
            return {
                "name": name, "pos_lo": plo, "pos_hi": phi,
                "light": light, "note": note,
                "pos_text": "%d%%~%d%%" % (round(plo * 100), round(phi * 100)),
            }
    return {"name": "未知", "pos_lo": 0.3, "pos_hi": 0.5, "light": "yellow",
            "note": "", "pos_text": "—"}


def score(ov: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 13 维归并成 0-100 分 + 六档建议。ov 为 westock.market_overview() 的返回。"""
    ov = ov or {}
    raw, status = {}, {}
    for d in ov.get("dims") or []:
        nm = d.get("name")
        v = d.get("score")
        if nm and isinstance(v, (int, float)):
            raw[nm] = float(v)
        if nm and d.get("status"):
            status[nm] = d["status"]

    groups: List[Dict[str, Any]] = []
    for gname, names, w in _GROUPS:
        vals = [raw[n] for n in names if n in raw]
        item: Dict[str, Any] = {
            "name": gname,
            "weight": w,
            "missing": [n for n in names if n not in raw],
        }
        if vals:
            item["score"] = round(sum(_norm(v) for v in vals) / len(vals), 1)
        else:
            item["score"] = None
        # status（如「暴跌(均线空头排列…)」）是上游唯一人话解释，必须带出去：
        # 单看 1~5 的分数没人知道 1 是什么意思，看状态文本才懂。
        item["dims"] = [
            {"name": n, "raw": raw[n], "score": round(_norm(raw[n]), 1),
             "status": status.get(n, "")}
            for n in names if n in raw
        ]
        groups.append(item)

    live = [g for g in groups if g["score"] is not None]
    if not live:
        # 一类都取不到 → 明确报不可用，不要返回 0 分（0 分=冰点，是最强的买入暗示）
        return {"ok": False, "error": "上游 13 维数据暂不可用"}

    wsum = sum(g["weight"] for g in live)
    total = sum(g["score"] * g["weight"] for g in live) / wsum
    total = round(total, 1)

    return {
        "ok": True,
        "score": total,
        "band": band_of(total),
        "groups": groups,
        "date": ov.get("date") or "",
        "request_date": ov.get("request_date") or "",
        "stale": bool(ov.get("stale")),
        "coverage": {"have": len(raw), "total": len(_ALL_DIMS)},
        # 权重被摊掉时如实标出，让「分数构成变了」这件事可见
        "reweighted": abs(wsum - 1.0) > 1e-6,
        # 上游自己的总评，仅作对照展示：量纲未公开（13 维求和是 32 而它给 15.58），
        # 不能与上面的 0-100 直接比较，也不参与任何计算。
        "upstream": {"raw": ov.get("raw_score"), "adj": ov.get("adj_score")},
    }


# 情绪面六维：与上面四类是两套切分，别混着读。
# 四类是「评分权重的归类」（估值/资金/情绪/技术），六维是「情绪面的展示切分」，
# 用于雷达图——估值与量能不在情绪面里，所以雷达只取 6 个维度，不是漏了数据。
# 每个维度都由现有 13 维里的真实维度支撑，没有凭空造的维度。
_RADAR: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("情绪", ("情绪指标",)),
    ("赚钱", ("个股宽度",)),                    # 普涨宽度 = 赚钱效应
    ("题材", ("板块宽度",)),                    # 上涨板块占比
    ("龙头", ("行业轮动",)),                    # 上游原文就是「头部在涨/尾部在跌」，即龙头效应
    ("轮动", ("风格轮动", "大小盘轮动")),
    ("趋势", ("技术指标", "短期趋势方向", "短期趋势强度",
              "中长期趋势方向", "中长期趋势强度")),
)


def radar(ov: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """情绪面六维（0~100），供雷达图。缺的维度返回 None，不补 0（0 会被读成一潭死水）。"""
    ov = ov or {}
    raw, status = {}, {}
    for d in ov.get("dims") or []:
        nm, v = d.get("name"), d.get("score")
        if nm and isinstance(v, (int, float)):
            raw[nm] = float(v)
        if nm and d.get("status"):
            status[nm] = d["status"]
    items = []
    for name, names in _RADAR:
        vals = [raw[n] for n in names if n in raw]
        items.append({
            "name": name,
            "score": round(sum(_norm(v) for v in vals) / len(vals), 1) if vals else None,
            "dims": [{"name": n, "raw": raw.get(n), "status": status.get(n, "")}
                     for n in names],
        })
    have = sum(1 for it in items if it["score"] is not None)
    return {"ok": have > 0, "items": items, "have": have, "total": len(_RADAR)}


# ===========================================================================
# 日序列落盘（趋势回看）——历史只能从今天往后攒，见 start_daily 说明
# ===========================================================================
K_LAST_DAY = "bullbear_last_date"   # 已落库的「上游画像日期」
K_LAST_TRY = "bullbear_last_try"    # 已尝试过的「本地日期」，防止同一天反复调 CLI
_bb_thread = None
_bb_stop = threading.Event()


def save_daily(ov: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把一次温度快照落进 bullbear_daily。日期取上游画像自己的 date。"""
    import store as _st

    r = score(ov)
    if not r.get("ok"):
        # 不写：0 分=冰点=最强买入暗示，绝不能用 0 表达「取数失败」
        return {"ok": False, "skipped": r.get("error") or "无有效维度"}
    d = r.get("date") or ""
    if not d:
        return {"ok": False, "skipped": "上游未返回数据日期，拒绝用今天顶替"}
    g = {x["name"]: x.get("score") for x in r.get("groups", [])}
    cov = r.get("coverage") or {}
    _st.bullbear_daily_save({
        "date": d,
        "score": r["score"],
        "val_score": g.get("估值"), "cap_score": g.get("资金"),
        "emo_score": g.get("情绪"), "tech_score": g.get("技术"),
        "band": (r.get("band") or {}).get("name"),
        "pos_text": (r.get("band") or {}).get("pos_text"),
        "have": cov.get("have"), "total": cov.get("total"),
        "upstream_raw": (r.get("upstream") or {}).get("raw"),
        "updated_at": time.time(),
    })
    _st.meta_set(K_LAST_DAY, d)
    return {"ok": True, "date": d, "score": r["score"]}


def history(days: int = 365) -> Dict[str, Any]:
    import store as _st

    days = max(1, min(int(days or 365), 1825))
    items = _st.bullbear_daily_list(days)
    return {"ok": True, "items": items, "count": len(items),
            "days": days, "enough": len(items) >= 2}


def _run_at() -> str:
    return os.environ.get("TICK_BULLBEAR_AT", "16:45")


def start_daily() -> bool:
    """每天收盘后落一次温度快照（daemon 线程）。

    冷启动说明：13 维历史从未存过，所以表只能从今天起攒，第一天只有 1 个点、
    画不出趋势线——前端按 count 判断并提示「积累中」，而不是画一条假线。
    """
    global _bb_thread
    if _bb_thread is not None and _bb_thread.is_alive():
        return False
    _bb_stop.clear()

    def _check(why: str):
        """取一次画像，上游日期变了才落库。非交易日上游日期不变，自然不落。"""
        try:
            import store as _st
            import westock as _w
            ov = _w.market_overview()
            d = (ov or {}).get("date") or ""
            if not d:
                print(f"[bullbear] {why}跳过：上游未返回数据日期")
                return
            if d == _st.meta_get(K_LAST_DAY, ""):
                print(f"[bullbear] {why}已有 {d} 的快照，跳过")
                return
            r = save_daily(ov)
            if r.get("ok"):
                print(f"[bullbear] {why}落库 {r['date']} 分数 {r['score']}")
            else:
                print(f"[bullbear] {why}跳过：{r.get('skipped')}")
        except Exception as e:
            print(f"[bullbear] {why}异常：{type(e).__name__}: {e}")

    def _loop():
        # 启动时先补一次（后台线程，不拖慢启动）：否则白天重启要等到收盘才有今天的点
        _check("启动补跑：")
        while not _bb_stop.is_set():
            try:
                now = _dt.datetime.now()
                hh, mm = (int(x) for x in _run_at().split(":"))
                import store as _st
                today = _dt.date.today().isoformat()
                # K_LAST_TRY 是本「地日期」的检查标记：当天已检查过就不再调上游，
                # 否则每 5 分钟都会白跑一次 CLI（日频任务不该这么烧上游）
                if (now.hour, now.minute) >= (hh, mm) and \
                        _st.meta_get(K_LAST_TRY, "") != today:
                    _st.meta_set(K_LAST_TRY, today)
                    _check("每日：")
                _bb_stop.wait(300)     # 5 分钟醒一次，日频任务不需要更密
            except Exception as e:
                print(f"[bullbear] 调度循环异常：{type(e).__name__}: {e}")
                _bb_stop.wait(300)

    _bb_thread = threading.Thread(target=_loop, daemon=True, name="bullbear-daily")
    _bb_thread.start()
    print(f"[bullbear] 每日落库调度已启动：每天 {_run_at()} 后检查一次"
          f"（环境变量 TICK_BULLBEAR_AT 可改）")
    return True
