"""全局风险过滤器（最大阴量 + J 值异常）+ 公告风险着色 端到端校验。

覆盖：
  A. 合成数据函数级：阴量剔除 / 阳量保留 / J 异常剔除 / 无数据放行 /
     tags 同步清理 / 开关关闭 / 无引擎放行（9 断言）
  B. HTTP 集成：/api/strategy_scan 的 diag.risk_filter 结构与统计；
     risk_filters=false（tune）时命中数 >= 开启时（过滤只减不增）
  C. 前端 noticeTone 真值表（Playwright evaluate）：风险→红、事件→橙、普通→默认

跑法（需先启动服务）：
    python3 tests/check_risk_filters.py
"""
import json
import os
import sys
import urllib.parse
import urllib.request

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = "http://127.0.0.1:8899"
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("✅" if ok else "❌"), name, detail)


# ============ A. 合成数据函数级 ============
from screener import _apply_risk_filters  # noqa: E402


class FakeHist:
    def __init__(self, data):
        self._data = data


def _mk_case():
    n = 60
    vol_a = np.full(n, 100.0)
    vol_a[55] = 1000.0
    base_o = 10 + np.sin(np.linspace(0, 9, n)) * 0.3
    base_c = base_o + 0.05
    base_o[55], base_c[55] = 10.2, 9.5            # 放量日=阴线
    vol_b = np.full(n, 100.0)
    vol_b[55] = 1000.0
    o2 = 10 + np.sin(np.linspace(0, 9, n)) * 0.3
    c2 = o2 + 0.05
    o2[55], c2[55] = 9.5, 10.8                     # 放量日=阳线
    cc = np.linspace(5, 50, n)                     # 连续拉升 → J 钉 100
    hh, ll, oo = cc * 1.01, cc * 0.99, cc * 0.995
    vol_c = np.full(n, 100.0)

    def mk(o, c, v):
        return {"open": np.asarray(o, float), "close": np.asarray(c, float),
                "high": np.maximum(o, c) + 0.05, "low": np.minimum(o, c) - 0.05,
                "volume": v}

    data = {"sh600000": mk(base_o, base_c, vol_a),
            "sh600001": mk(o2, c2, vol_b),
            "sh600002": {"open": oo, "close": cc, "high": hh, "low": ll, "volume": vol_c}}
    rows = [{"code": "sh600000", "name": "阴量票"},
            {"code": "sh600001", "name": "正常票"},
            {"code": "sh600002", "name": "J异常票"},
            {"code": "sz000001", "name": "无数据票"}]
    return FakeHist(data), rows


def unit_tests():
    fh, rows = _mk_case()
    tags = {r["code"]: ["ma_bull"] for r in rows}
    out, tags2, diag2 = _apply_risk_filters(rows, tags, {}, hist=fh)
    codes = [r["code"] for r in out]
    check("A1 阴量票被剔除", "sh600000" not in codes,
          diag2["risk_filter"]["removed"][0]["reason"] if diag2["risk_filter"]["removed"] else "")
    check("A2 阳量票保留", "sh600001" in codes)
    check("A3 J异常票被剔除", "sh600002" not in codes)
    check("A4 无历史数据票放行", "sz000001" in codes)
    check("A5 tags 同步清理", "sh600000" not in tags2)
    rf = diag2["risk_filter"]
    check("A6 分类计数正确", rf["max_vol_down"] == 1 and rf["j_anomaly"] == 1,
          f"max_vol_down={rf['max_vol_down']} j_anomaly={rf['j_anomaly']}")
    out2, _, d3 = _apply_risk_filters(rows, dict(tags), {}, hist=fh, risk_filters=False)
    check("A7 关闭开关全保留", len(out2) == 4 and d3["risk_filter"]["enabled"] is False)
    out3, _, _ = _apply_risk_filters(rows, dict(tags), {}, hist=None)
    check("A8 无引擎全保留", len(out3) == 4)
    check("A9 剔除理由可读", all("reason" in x and x["reason"] for x in rf["removed"]))


# ============ B. HTTP 集成 ============
def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read().decode())


def http_tests():
    d = get("/api/strategy_scan?keys=pullback_ma20&mode=union&limit=500")
    rf = (d.get("diag") or {}).get("risk_filter") or {}
    check("B1 diag.risk_filter 存在", isinstance(rf, dict) and "enabled" in rf,
          f"enabled={rf.get('enabled')} max_vol_down={rf.get('max_vol_down')} j_anomaly={rf.get('j_anomaly')}")
    check("B2 默认开启", rf.get("enabled") is True)
    removed = rf.get("removed") or []
    ok_shape = all(("code" in x and "name" in x and "reason" in x) for x in removed)
    check("B3 removed 结构正确", ok_shape, f"n={len(removed)}")
    check("B3b 真实数据有剔除", rf.get("max_vol_down", 0) + rf.get("j_anomaly", 0) > 0,
          f"统计剔除 {rf.get('max_vol_down', 0)}+{rf.get('j_anomaly', 0)}（明细截前50）")
    check("B3c 明细≤统计且≤50", len(removed) <= rf.get("max_vol_down", 0) + rf.get("j_anomaly", 0)
          and len(removed) <= 50)
    total_on = d.get("total", 0)

    tune = urllib.parse.quote(json.dumps({"risk_filters": False}))
    d2 = get(f"/api/strategy_scan?keys=pullback_ma20&mode=union&limit=500&tune={tune}")
    rf2 = (d2.get("diag") or {}).get("risk_filter") or {}
    check("B4 tune 关闭生效", rf2.get("enabled") is False and rf2.get("removed") == [])
    check("B5 关闭后命中数 > 开启时", d2.get("total", 0) > total_on,
          f"开={total_on} 关={d2.get('total', 0)} 剔除={d2['total'] - total_on}")


# ============ C. 前端 noticeTone 真值表 ============
def tone_tests():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto("about:blank")
            # 直接注入函数定义（与 index.html 保持一致的源码）
            html = open(os.path.join(os.path.dirname(__file__), "..", "web", "index.html"),
                        encoding="utf-8").read()
            import re
            m = re.search(r"function noticeTone\(t\)\{.*?\n\}", html, re.S)
            if not m:
                check("C1 提取 noticeTone", False)
                b.close()
                return
            pg.evaluate(";" + m.group(0))
            cases = [
                ("关于控股股东部分股份质押的公告", "var(--down)"),
                ("股东减持计划实施完毕", "var(--down)"),
                ("股票交易异常波动停牌核查", "var(--down)"),
                ("以集中竞价方式回购公司股份", "var(--warn)"),
                ("重大资产重组停牌进展", "var(--down)"),   # 停牌属风险优先
                ("召开年度股东大会通知", ""),
                ("对外投资设立子公司并完成工商登记合作", "var(--warn)"),
            ]
            allok = True
            for title, expect in cases:
                got = pg.evaluate(f"noticeTone({json.dumps(title)})")
                if got != expect:
                    allok = False
                    print(f"   ❌ {title} → {got}（期望 {expect}）")
            check("C1 noticeTone 真值表 7/7", allok)
            b.close()
    except Exception as e:  # noqa: BLE001
        check("C1 noticeTone 真值表", False, f"Playwright 异常: {e}")


unit_tests()
http_tests()
tone_tests()

n_pass = sum(1 for _, ok in results if ok)
print(f"\n通过 {n_pass}/{len(results)}")
sys.exit(0 if n_pass == len(results) else 1)
