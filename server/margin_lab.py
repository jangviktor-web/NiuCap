#!/usr/bin/env python3
"""两融杠杆：沪深融资融券余额、杠杆率与历史序列
==================================================

数据源：东方财富数据中心 RPTA_RZRQ_LSHJ（公开接口，guchacha.com 同源）。
实测 2026-10-30：一次 500 条、共 9 页 ≈ 2010 年起全历史，字段：
  DIM_DATE  日期        NEW    两融余额（亿元）
  RZYE      融资余额（元）  RQYE   融券余额（元）
  LTSZ      流通市值（元）  RZYEZB 融资余额占流通市值比（%，即杠杆率）

口径：
  - 杠杆率 = 融资余额 / 流通市值（借钱做多的资金相对盘子的比重），
    是风险偏好的风向标：快速攀升=行情亢奋，回落=去杠杆。
  - 余额本身长期通胀（10 年翻了数倍），**历史分位用「近20日余额变化%」
    而不是余额绝对值**——绝对值分位会常年钉死在高位，失去参考意义。
  - 恐贪子指标从这里取：近20日融资余额变化% 的历史分位，加仓=贪婪端。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

TTL = 21600         # 6 小时：两融 T+1 日频，盘中刷新永远拿不到新值，
                    # 白烧东财数据中心还容易触发限流。当天最多 4 次足够。
K_CACHE = "margin_cache"
_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageSize=500&sortColumns=dim_date"
        "&sortTypes=-1&pageNumber={page}")
_YI = 1e8  # 元 → 亿


def fetch_all(max_pages: int = 9) -> List[Dict[str, Any]]:
    """拉全历史，升序返回。字段裁到用得上的几列。"""
    import requests

    rows: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        r = requests.get(_URL.format(page=page),
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        data = ((r.json() or {}).get("result") or {}).get("data") or []
        if not data:
            break
        for d in data:
            ltsz = d.get("LTSZ")
            rz = (d.get("RZYE") or 0) / _YI
            rq = (d.get("RQYE") or 0) / _YI
            lev = (rz / (ltsz / _YI) * 100) if ltsz else None
            # 早期年份 LTSZ 口径不稳（实测 2010 出现 4e-5% 的怪值），量级过滤
            if lev is not None and not (0 < lev < 50):
                lev = None
            rows.append({
                "date": (d.get("DIM_DATE") or "")[:10],
                # NEW 字段口径不明（4041 亿≠两融余额），两融余额=融资+融券 自算
                "rzrq": rz + rq,
                "rz": rz, "rq": rq,
                "ltsz": ltsz,
                "lev": lev,  # 杠杆率 %
            })
    rows.sort(key=lambda x: x["date"])
    return rows


def _chg(vals: List[float], n: int) -> Optional[float]:
    """近 n 日变化%。样本不足返回 None。"""
    if len(vals) <= n or not vals[-n - 1]:
        return None
    base = vals[-n - 1]
    return (vals[-1] / base - 1) * 100 if base else None


def _pct(cur: Optional[float], vals: List[Optional[float]]) -> Optional[float]:
    vs = [v for v in vals if v is not None]
    if cur is None or len(vs) < 30:
        return None
    return sum(1 for v in vs if v < cur) / len(vs)


def build() -> Dict[str, Any]:
    import store as _st

    now = time.time()
    raw = _st.meta_get(K_CACHE, "")
    if raw:
        try:
            c = json.loads(raw)
            if now - float(c.get("at") or 0) < TTL and c.get("data"):
                return dict(c["data"], cached=True)
        except Exception:
            pass

    try:
        rows = fetch_all()
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": f"东财两融接口取数失败：{type(e).__name__}: {e}"}
    if len(rows) < 40:
        return {"ok": False, "error": f"两融历史仅 {len(rows)} 条，不足以计算"}

    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else {}
    rz_series = [r["rz"] for r in rows]
    chg20_all = [_chg(rz_series[:i + 1], 20) for i in range(len(rz_series))]
    c20, c7, c30 = _chg(rz_series, 20), _chg(rz_series, 7), _chg(rz_series, 30)
    p20 = _pct(c20, chg20_all)

    data = {
        "ok": True,
        "date": last["date"],
        "latest": {
            "rzrq": last["rzrq"],
            "rz": last["rz"], "rq": last["rq"],
            "lev": last["lev"],
            "day_chg": (last["rzrq"] - prev.get("rzrq", 0))
                       if prev.get("rzrq") else None,
            "chg7": c7, "chg30": c30, "chg20": c20,
        },
        "chg20_pct": p20,                # 恐贪子指标：近20日融资余额变化的历史分位
        "hist_days": len(rows),
        "since": rows[0]["date"],
        # 前端画线用：只带日期与两个序列，别把 4500 行全量字段塞给浏览器
        "series": [[r["date"], r["rzrq"], r["lev"]] for r in rows],
        "caveats": [
            "数据源：东方财富数据中心（T+1 更新，最新一条是上一交易日）。",
            "杠杆率 = 融资余额 / 流通市值。融券余额代表做空规模，但 A 股融券池极小"
            "（282 亿 vs 融资 2.36 万亿），情绪信号以融资为主。",
            "余额绝对值长期通胀，历史分位用「近20日融资余额变化%」而非余额本身——"
            "绝对值分位会常年钉死在高位。",
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
    """纯逻辑自检：变化率、分位、截断。"""
    bad = 0

    def ck(ok: bool, msg: str) -> None:
        nonlocal bad
        if not ok:
            bad += 1
            print(f"  ❌ {msg}")

    ck(_chg([100, 110], 20) is None, "样本不足近20日变化应为 None")
    ck(abs(_chg([100] + [100.0] * 20 + [110], 20) - 10.0) < 1e-9,
       "近20日变化 100→110 应为 10%")
    ck(_chg([100] + [100.0] * 20 + [110], 20) is not None, "恰好 21 点可算")
    ck(_pct(5.0, [1.0] * 10) is None, "分位样本不足应拒绝")
    ck(_pct(5.0, [1.0] * 30) == 1.0, "全小于时分位 1.0")
    ck(_pct(None, [1.0] * 30) is None, "当前值缺失应 None")
    print(f"margin_lab.selfcheck: {'OK' if bad == 0 else str(bad) + ' 项失败'}")
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if selfcheck() else 0)
