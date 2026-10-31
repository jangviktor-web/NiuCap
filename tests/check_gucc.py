"""guchacha 搬运四件套自检：两融 / 巴菲特指标 / 行业拥挤度 / 估值计算器。

2026-10-30 新增。守关键口径：
  1. 两融余额=融资+融券（东财 NEW 字段口径不明，不能用），与 guchacha 2.38 万亿对读；
  2. 巴菲特指标只统计个股（指数 total_cap 是怪值），与 guchacha 76% 交叉验证；
  3. 拥挤度分位不足 30 天不给综合值（攒分位不编分位）；
  4. 估值计算器永续 g2 ≥ 折现率必须拒绝（公式发散不是估值）。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
CHROME = os.environ.get("CHROME", "/usr/bin/chromium")
res = []


def rec(name, ok, detail=""):
    res.append(bool(ok))
    print(("✅" if ok else "❌") + f" {name}  {detail}")


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        return json.load(r)


print("── A 纯逻辑（不联网）──")
import margin_lab as mgl   # noqa: E402
import crowding as crd     # noqa: E402
rec("A margin selfcheck", mgl.selfcheck() == 0)
rec("A crowding selfcheck", crd.selfcheck() == 0)

print("── B 后端 API ──")
try:
    d = api("/api/margin")
    rec("B 两融接口", d.get("ok") is True, d.get("error", ""))
    L = d.get("latest") or {}
    rec("B 两融余额=融资+融券",
        abs(L.get("rzrq", 0) - (L.get("rz", 0) + L.get("rq", 0))) < 1e-6,
        f"{L.get('rzrq'):.0f} 亿")
    rec("B 两融量级合理（1~5万亿）",
        10000 < L.get("rzrq", 0) < 50000, f"{L.get('rzrq') / 10000:.2f} 万亿")
    rec("B 杠杆率量级（1%~8%）",
        1 < (L.get("lev") or 0) < 8, f"{L.get('lev'):.2f}%")
    rec("B 历史深度 >1000 天", (d.get("hist_days") or 0) > 1000,
        f"{d.get('hist_days')} 天，{d.get('since')} 起")
except Exception as e:  # noqa: BLE001
    rec("B 两融接口可用", False, f"{type(e).__name__}: {e}")

try:
    d = api("/api/market_vitals")
    bf = d.get("buffett") or {}
    rec("B 巴菲特指标", bool(bf.get("ratio")),
        f"总市值 {bf.get('total_mv', 0) / 1e4:.1f} 万亿 / GDP {bf.get('gdp_ttm', 0) / 1e4:.1f} 万亿")
    rec("B 巴菲特量级（0.3~1.5）", 0.3 < (bf.get("ratio") or 0) < 1.5,
        f"{(bf.get('ratio') or 0) * 100:.1f}%（guchacha 76.0% 交叉验证）")
    rec("B 攒分位纪律",
        (d.get("buffett_pct") is None) == (d.get("buffett_hist_days", 0) < 30),
        f"hist={d.get('buffett_hist_days')}")
except Exception as e:  # noqa: BLE001
    rec("B 巴菲特可用", False, f"{type(e).__name__}: {e}")

try:
    d = api("/api/crowding")
    rec("B 拥挤度接口", d.get("ok") is True, d.get("error", ""))
    items = d.get("items") or []
    rec("B 板块数 >=50", len(items) >= 50, f"{len(items)} 个")
    rec("B 成交占比合计=100%",
        abs(sum(x.get("to_ratio") or 0 for x in items) - 1) < 0.01)
    rec("B 分位不足不给综合值",
        all(x.get("crowd") is None for x in items) == (not d.get("enough")),
        f"enough={d.get('enough')}")
except Exception as e:  # noqa: BLE001
    rec("B 拥挤度可用", False, f"{type(e).__name__}: {e}")

print("── C 页面渲染 ──")
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    br = p.chromium.launch(headless=True, executable_path=CHROME)
    pg = br.new_page(viewport={"width": 1440, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_timeout(1200)
    pg.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg.wait_for_selector("#bbBody .bbhero, #bbBody .empty", timeout=180000)
    pg.wait_for_timeout(4000)
    d = pg.evaluate("""()=>{
      var g=id=>document.getElementById(id);
      var mg=g('mgBody'), cr=g('crBody'), val=g('valBody'), fgbox=g('bbBody');
      return {
        mgHero: mg? (mg.querySelector('.bbhero .n')||{}).textContent : null,
        mgChart: !!g('mgChart'),
        crRows: cr? cr.querySelectorAll('tbody tr').length : 0,
        crCells: cr? (cr.querySelector('tbody tr')||{innerText:''}).innerText : '',
        valCards: val? val.querySelectorAll('.vout').length : 0,
        vDcf: val? (val.querySelector('#vDcfOut')||{textContent:''}).textContent : '',
        fgRows: fgbox? Array.from(fgbox.querySelectorAll('.bbdim'))
          .filter(x=>x.innerText.indexOf('两融情绪')>=0).length : 0,
        text: document.body.innerText.replace(/\\s+/g,' ')
      };
    }""")
    rec("C 两融主读数渲染", bool(d["mgHero"]), str(d["mgHero"]))
    rec("C 两融历史图画出", d["mgChart"])
    rec("C 拥挤度表渲染", d["crRows"] >= 50, f"rows={d['crRows']}")
    rec("C 拥挤度攒分位标注", "攒分位中" in d["text"])
    rec("C 估值计算器三模型", d["valCards"] == 3, f"cards={d['valCards']}")
    rec("C DCF 默认参数有输出", "内在价值" in d["vDcf"] or "不收敛" in d["vDcf"],
        d["vDcf"][:60])
    rec("C 恐贪含两融子指标行", d["fgRows"] >= 1)
    rec("C 恐贪 6 项齐全", "6/6 项子指标" in d["text"], "")

    pg2 = br.new_page(viewport={"width": 390, "height": 844})
    pg2.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg2.wait_for_timeout(1200)
    pg2.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg2.wait_for_timeout(3000)
    ov = pg2.evaluate("()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
    rec("C 390px 无横向溢出", ov["sw"] <= ov["cw"] + 1,
        f'scrollW={ov["sw"]} clientW={ov["cw"]}')

    mine = [e for e in errs if ("401" not in e and "Failed to load resource" not in e)]
    rec("C 无 JS 报错", len(mine) == 0, "; ".join(mine[:3]))
    br.close()

ok = sum(res)
print(f"\n{ok}/{len(res)} PASS")
sys.exit(0 if ok == len(res) else 1)
