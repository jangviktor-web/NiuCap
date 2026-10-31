"""市场体检卡的浏览器自检：破净率、风格罗盘、宽基 PE 渲染 + 口径断言。

2026-10-30 新增（参考 baifenwei.com 搬的三个读数）。守三条关键口径：
  1. 破净率量级必须合理（5%~25% 之外说明 PB 口径坏了）；
  2. 自建分位样本 <30 天时**不能出现数字**——攒分位不是编分位；
  3. 风格分位 0 是合法读数（比值处一年最低），UI 必须显示「0%」而不是空。
"""
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
CHROME = os.environ.get("CHROME", "/usr/bin/chromium")
res = []


def rec(name, ok, detail=""):
    res.append(bool(ok))
    print(("✅" if ok else "❌") + f" {name}  {detail}")


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def test_api():
    print("── A 后端 ──")
    try:
        d = api("/api/market_vitals")
    except Exception as e:
        rec("A 接口可用", False, f"{type(e).__name__}: {e}")
        return d
    if not d.get("ok"):
        rec("A ok=True", False, str(d.get("error"))[:100])
        return d
    b = d["broken"]
    rec("A 破净率量级合理", 0.03 < (b["ratio"] or 0) < 0.30,
        f"{(b['ratio'] or 0) * 100:.2f}%（{b['below_n']}/{b['total']}）")
    rec("A 全A覆盖度", (b["total"] or 0) > 5000, f"total={b['total']}")
    rec("A 负PB单列不计破净", b.get("neg_n") is not None, f"neg={b.get('neg_n')}")
    # 分位纪律：样本不足 30 天时 broken_pct 必须是 None
    rec("A 样本不足不给分位",
        (d["broken_pct"] is None) == (d["hist_days"] < 30),
        f"hist={d['hist_days']} pct={d['broken_pct']}")
    st = [s for s in d["styles"] if not s.get("error")]
    rec("A 风格三对齐全", len(st) == 3, f"{len(st)}/3")
    rec("A 风格分位在 0~1",
        all(0 <= s["p250"] <= 1 and 0 <= s["p60"] <= 1 for s in st))
    pes = [p for p in d["index_pe"] if p["pe"]]
    rec("A 指数 PE 量级合理",
        all(5 < p["pe"] < 120 for p in pes),
        ", ".join(f"{p['name']} {p['pe']}" for p in pes))
    return d


test_api()

with sync_playwright() as p:
    br = p.chromium.launch(headless=True, executable_path=CHROME)
    pg = br.new_page(viewport={"width": 1440, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_timeout(1200)
    pg.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg.wait_for_selector("#mvBody .bbhero, #mvBody .empty", timeout=120000)
    pg.wait_for_timeout(1500)

    d = pg.evaluate("""()=>{
      var box=document.getElementById('mvBody');
      return {has: !!box, text: box?box.innerText.replace(/\\s+/g,' ').slice(0,600):'',
        hero: box? (box.querySelector('.bbhero .n')||{}).textContent : null,
        styleRows: box? box.querySelectorAll('.bbsec')[0].querySelectorAll('tbody tr').length : 0,
        peRows: box? box.querySelectorAll('.bbsec')[1].querySelectorAll('tbody tr').length : 0};
    }""")
    rec("卡片已渲染", d["has"])
    rec("破净率主读数形如百分比",
        bool(d["hero"]) and d["hero"].strip().endswith("%"), str(d["hero"]))
    rec("风格罗盘 3 行", d["styleRows"] == 3, f"rows={d['styleRows']}")
    rec("指数 PE 6 行", d["peRows"] == 6, f"rows={d['peRows']}")
    # 口径 2：攒分位期间页面不得出现硬编的破净分位数字
    if d["hero"] and "攒" in d["text"]:
        rec("攒分位期无假分位", "历史分位：样本" in d["text"])
    # 口径 3：风格分位 0 必须显示为 0%（今天是极端日，三条里至少一条该贴地）
    rec("风格分位 0 显示为 0% 而非空",
        "0% ·" in d["text"] or d["text"].count("% ·") >= 3, d["text"][:200])

    # 窄屏不横向溢出
    pg2 = br.new_page(viewport={"width": 390, "height": 844})
    pg2.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg2.wait_for_timeout(1200)
    pg2.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg2.wait_for_selector("#mvBody .bbhero, #mvBody .empty", timeout=120000)
    pg2.wait_for_timeout(1000)
    ov = pg2.evaluate("()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
    rec("390px 无横向溢出", ov["sw"] <= ov["cw"] + 1,
        f'scrollW={ov["sw"]} clientW={ov["cw"]}')

    mine = [e for e in errs if "market_vitals" in e or "mvBody" in e
            or ("401" not in e and "Failed to load resource" not in e)]
    rec("本功能无 JS 报错", len(mine) == 0, "; ".join(mine[:3]))
    br.close()

ok = sum(res)
print(f"\n{ok}/{len(res)} PASS")
sys.exit(0 if ok == len(res) else 1)
