#!/usr/bin/env python3
"""行业拥挤度：成交占比 / 换手 / 动量（当日读数 + 自建历史分位）
==================================================

参考 guchacha.com /crowding 的口径，数据源用面板已有的 westock.sector_rank
（90 个东财行业板块，零新数据源）：

  成交占比  = 板块成交额 ÷ 全市场板块成交额合计 —— 资金是否扎堆
  换手率    = 上游 turnover_rate              —— 交易是否过热
  动量      = 当日涨跌幅                        —— 是否追涨（20周动量上游无源，砍）
  综合拥挤度 = 三个**历史分位**的均值（guchacha 口径）

分位纪律（与 market_vitals 同款）：分位需要历史，每天收盘后懒触发落一行，
自建样本不足 30 天时**不给分位、不给综合拥挤度**——攒分位，不编分位。
guchacha 的 20 周动量分位与「超配」（需板块流通市值占比）本环境无源，砍掉。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

TTL = 1800          # 30 分钟：拥挤度是结构指标（成交占比/换手占比），
                    # 盘中会累积变化但看盘级精度就够；落库本身日频、有防重
K_CACHE = "crowding_cache"
K_DAILY = "crowding_daily_json"       # meta JSON 列表，每日一行，上限 400 天
CAPTURE_AFTER = (15, 30)


def _snapshot() -> List[Dict[str, Any]]:
    """当日板块读数（复用上游缓存，不重复打 CLI）。"""
    import westock as _w
    items = _w.sector_rank(200)
    rows = []
    for it in items:
        if not it.get("name") or not it.get("turnover"):
            continue
        rows.append({
            "name": it["name"],
            "to": it["turnover"],              # 万元
            "tr": it.get("turnover_rate"),     # %
            "chg": it.get("change_pct"),       # %
        })
    return rows


def _pct(cur: Optional[float], vals: List[float]) -> Optional[float]:
    if cur is None or len(vals) < 30:
        return None
    return sum(1 for v in vals if v < cur) / len(vals)


def _history() -> List[Dict[str, Any]]:
    import store as _st
    try:
        return json.loads(_st.meta_get(K_DAILY, "[]") or "[]")
    except Exception:
        return []


def build(force: bool = False) -> Dict[str, Any]:
    import store as _st

    now = time.time()
    if not force:
        raw = _st.meta_get(K_CACHE, "")
        if raw:
            try:
                c = json.loads(raw)
                if now - float(c.get("at") or 0) < TTL and c.get("data"):
                    return dict(c["data"], cached=True)
            except Exception:
                pass

    try:
        rows = _snapshot()
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": f"板块行情取数失败：{type(e).__name__}: {e}"}
    if len(rows) < 10:
        return {"ok": False, "error": f"板块仅 {len(rows)} 个，数据异常"}
    total_to = sum(r["to"] for r in rows) or 1

    # 历史序列 → 每板块各指标的样本
    hist = _history()
    by_name: Dict[str, Dict[str, List[float]]] = {}
    for h in hist:
        for nm, r in (h.get("rows") or {}).items():
            s = by_name.setdefault(nm, {"to_ratio": [], "tr": [], "chg": []})
            if r.get("to_ratio"):
                s["to_ratio"].append(r["to_ratio"])
            if r.get("tr"):
                s["tr"].append(r["tr"])
            if r.get("chg") is not None:
                s["chg"].append(r["chg"])

    out = []
    for r in sorted(rows, key=lambda x: -x["to"]):
        ratio = r["to"] / total_to
        s = by_name.get(r["name"], {})
        p_to = _pct(ratio, s.get("to_ratio", []))
        p_tr = _pct(r["tr"], s.get("tr", []))
        p_chg = _pct(r["chg"], s.get("chg", []))
        ps = [p for p in (p_to, p_tr, p_chg) if p is not None]
        out.append({
            "name": r["name"], "to": r["to"], "to_ratio": ratio,
            "tr": r["tr"], "chg": r["chg"],
            "p_to": p_to, "p_tr": p_tr, "p_chg": p_chg,
            # 综合拥挤度 = 三分位均值；有分位才给（guchacha 口径）
            "crowd": round(sum(ps) / len(ps) * 100) if len(ps) == 3 else None,
        })

    # 懒触发落库：过 15:30 且今天没落过 → 存今天一行
    import datetime as _dt
    nowdt = _dt.datetime.now()
    today = nowdt.strftime("%Y-%m-%d")
    captured = False
    if (nowdt.hour, nowdt.minute) >= CAPTURE_AFTER and \
            (not hist or hist[-1].get("date") != today):
        try:
            hist = [x for x in hist if x.get("date") != today]
            hist.append({"date": today, "rows": {
                r["name"]: {"to_ratio": r["to_ratio"], "tr": r["tr"],
                            "chg": r["chg"]} for r in out}})
            _st.meta_set(K_DAILY, json.dumps(hist[-400:], ensure_ascii=False))
            captured = True
        except Exception:
            pass

    data = {
        "ok": True, "date": today, "hist_days": len(hist) + (0 if captured else 0),
        "items": out, "n_boards": len(out),
        "enough": len(hist) >= 30,
        "capture": {"captured": captured},
        "caveats": [
            "数据源：面板已有板块行情（东财口径 90 个行业板块），"
            "与 guchacha 的申万一级 31 行业不是同一套板块，**排名只能内部比**，"
            "不要和申万口径直接对读。",
            "综合拥挤度 = 成交占比/换手/动量三个**历史分位**的均值；"
            "分位是自建样本（每天收盘后落一行），攒够 30 天才给——"
            "今天起开始攒，此前显示「攒分位中」。",
            "guchacha 的 20 周动量分位与「超配」（需板块流通市值占比）本环境无源，砍掉。",
            "红=拥挤、绿=冷清是参考站配色语义；拥挤不等于会跌，只说明交易扎堆。",
        ],
    }
    try:
        _st.meta_set(K_CACHE, json.dumps({"at": now, "data": data},
                                         ensure_ascii=False))
    except Exception:
        pass
    data["cached"] = False
    return data


def selfcheck() -> int:
    bad = 0

    def ck(ok: bool, msg: str) -> None:
        nonlocal bad
        if not ok:
            bad += 1
            print(f"  ❌ {msg}")

    ck(_pct(1.0, [0.5] * 10) is None, "样本不足 30 应拒绝给分位")
    ck(_pct(1.0, [0.5] * 30) == 1.0, "全小于时分位 1.0")
    ck(_pct(None, [0.5] * 30) is None, "当前值缺失应 None")
    print(f"crowding.selfcheck: {'OK' if bad == 0 else str(bad) + ' 项失败'}")
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if selfcheck() else 0)
