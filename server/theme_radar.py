"""题材雷达（融合去重）——蒸馏 easy-stock sector.radar_fusion / radar_strength

- 沙箱东财(push2*.eastmoney) 被拦截 → 基于 westock.sector_rank() 的「行业板块」做四维评分：
  强度(涨幅) + 资金(主力净流入) + 宽度(上涨占比) + 持续(20日净流)。
- 融合去重 fuse_boards()：名称归一（去「板块/行业/概念」后缀）+ 同口径合并，
  结构上预留「行业 + 概念」双口径融合（概念走东财，沙箱不可用，待开放）。
- 降级：westock CLI 不可用 → sector_rank 返回 [] → 接口返回 source_error，不 500。
"""

import re
from typing import Any, Dict, List

import westock as ws

# 四维权重（强度/资金/宽度/持续）
_W_INTENSITY, _W_FUND, _W_WIDTH, _W_DUR = 0.35, 0.30, 0.20, 0.15


def _norm(name: str) -> str:
    """板块名称归一：去空格、去「板块/行业/概念/指数」后缀，便于跨口径去重。"""
    n = (name or "").strip()
    return re.sub(r"(板块|行业|概念|指数)$", "", n)


def _up_width(up_count) -> float:
    """'9/13' → 上涨占比 0~1；解析失败返回 0。"""
    try:
        a, c = str(up_count).split("/")
        a, c = int(a), int(c)
        return a / c if c > 0 else 0.0
    except Exception:
        return 0.0


def _score(b: Dict[str, Any]) -> Dict[str, float]:
    cp = float(b.get("change_pct", 0) or 0)
    net = float(b.get("main_net", 0) or 0)         # 万元
    net20 = float(b.get("main_net_20d", 0) or 0)    # 万元
    width = _up_width(b.get("up_count", ""))

    # 各自归一 0~100
    s_intensity = min(100.0, max(0.0, (cp + 5.0) / 10.0 * 100.0))  # -5%~+5% → 0~100
    s_fund = min(100.0, max(0.0, net / 50000.0 * 100.0))          # 5亿封顶
    s_width = width * 100.0
    s_dur = min(100.0, max(0.0, net20 / 200000.0 * 100.0))        # 20亿封顶
    score = (_W_INTENSITY * s_intensity + _W_FUND * s_fund
             + _W_WIDTH * s_width + _W_DUR * s_dur)
    return {"score": round(score, 1), "s_intensity": round(s_intensity, 1),
            "s_fund": round(s_fund, 1), "s_width": round(s_width, 1),
            "s_dur": round(s_dur, 1)}


def fuse_boards(*lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """多口径板块融合去重：同名（归一后）合并，强度取较大者、资金累加。"""
    merged: Dict[str, Dict[str, Any]] = {}
    for lst in lists:
        for b in lst:
            key = _norm(b.get("name", ""))
            if not key:
                continue
            if key in merged:
                m = merged[key]
                m["change_pct"] = max(float(m.get("change_pct", 0) or 0),
                                     float(b.get("change_pct", 0) or 0))
                m["main_net"] = (float(m.get("main_net", 0) or 0)
                                 + float(b.get("main_net", 0) or 0))
                m["_sources"] = m.get("_sources", 1) + 1
            else:
                b2 = dict(b)
                b2["_sources"] = 1
                merged[key] = b2
    return list(merged.values())


def build_radar(limit: int = 40) -> Dict[str, Any]:
    """行业板块 → 融合去重 → 四维评分排序。"""
    import datetime as _dt
    try:
        boards = ws.sector_rank(limit=max(limit * 2, 30))
    except Exception as e:
        return {"ok": False, "source_error": f"{type(e).__name__}: {e}", "boards": []}
    if not boards:
        return {"ok": False,
                "source_error": "westock 行业板块返回空（eltdx CLI 不可用？）",
                "boards": []}
    fused = fuse_boards(boards)  # 目前仅行业口径；概念待开放后并入
    for b in fused:
        b.update(_score(b))
    fused.sort(key=lambda x: -x["score"])
    return {"ok": True, "date": _dt.date.today().isoformat(),
            "count": len(fused), "boards": fused[:limit]}


def selfcheck() -> int:
    fails = 0

    def rec(name, ok, detail=""):
        nonlocal fails
        if not ok:
            fails += 1
            print(f"  ❌ {name} {detail}")
        else:
            print(f"  ✅ {name}")

    rec("名称归一 去板块后缀", _norm("证券板块") == "证券")
    rec("名称归一 去概念后缀", _norm("人工智能概念") == "人工智能")
    rec("上涨占比 9/13", abs(_up_width("9/13") - 9 / 13) < 1e-9)
    rec("上涨占比 解析失败=0", _up_width("x") == 0.0)
    # 融合去重：同名合并、资金累加
    fused = fuse_boards(
        [{"name": "券商板块", "change_pct": 3.0, "main_net": 100.0},
         {"name": "券商概念", "change_pct": 2.0, "main_net": 50.0}])
    rec("融合去重 合并为 1 条", len(fused) == 1, f"得 {len(fused)}")
    rec("融合去重 资金累加", abs(fused[0]["main_net"] - 150.0) < 1e-9)
    # 评分：强板块应高于弱板块
    strong = _score({"change_pct": 5.0, "main_net": 80000, "up_count": "20/20",
                     "main_net_20d": 300000})
    weak = _score({"change_pct": 0.0, "main_net": 0, "up_count": "1/20",
                   "main_net_20d": 0})
    rec("评分 强>弱", strong["score"] > weak["score"],
        f"{strong['score']} vs {weak['score']}")
    return fails


if __name__ == "__main__":
    print("theme_radar selfcheck:")
    n = selfcheck()
    print("FAIL" if n else "OK")
