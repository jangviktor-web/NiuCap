# -*- coding: utf-8 -*-
"""个股页估值计算器检查（guchacha 四件套 → 个股页优化）。

验证三件事，每条都是"改了会静默坏掉"的：
  1. 卡片存在：renderStock 后动态追加（stockWrap 整体被覆盖，不能静态写死）
  2. 预填正确：股价/EPS/股本/目标PE 来自 quote 快照，与接口返回逐项对账
  3. 计算输出：DCF 输出不是「参数不全」（预填齐了 Share/Price 就应出数）

用法：cd /workspace/tick-stock-panel && python3 tests/check_stock_valcalc.py
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
CHROME = "/usr/bin/chromium"

passes, fails = [], []

def ck(name, cond, extra=""):
    (passes if cond else fails).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          ((" :: " + str(extra)) if (extra and not cond) else ""))

def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=CHROME,
                              args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(700)
        # 先登录：个股页的「AI 深度分析」卡会弹登录框挡住截图
        pg.evaluate("""async ()=>{
          await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username:'bltest',password:'bltest123',display:'x'})}).catch(()=>{});
          await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username:'bltest',password:'bltest123'})});}""")
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)
        # 进个股页（复用 smoke_stock_viz 的入口）
        pg.evaluate("()=>{document.querySelector('[data-tab=\"stock\"]').click(); openStock('000001');}")
        pg.wait_for_selector("#ckline canvas", timeout=40000)
        pg.wait_for_selector("#stockValCard", timeout=20000)
        pg.wait_for_timeout(800)

        # 1 卡片存在且不重复
        ck("1 个股页计算器卡片存在", pg.locator("#stockValCard").count() == 1)

        # 2 预填与 quote 快照对账
        q = pg.evaluate("fetch('/api/kline?code=000001&period=day&count=30').then(r=>r.json()).then(d=>d.quote||{})")
        pr = q.get("price")
        filled_price = pg.eval_on_selector("#svPrice", "el=>parseFloat(el.value)")
        ck("2a 股价预填=quote.price", pr and abs(filled_price - pr) < 0.005,
           f"quote={pr} filled={filled_price}")
        if pr and q.get("pe") and q["pe"] > 0:
            eps_exp = pr / q["pe"]
            eps_fill = pg.eval_on_selector("#svEps", "el=>parseFloat(el.value)")
            ck("2b EPS 预填=price/pe", abs(eps_fill - eps_exp) < max(0.01, eps_exp*0.01),
               f"exp={eps_exp:.4f} fill={eps_fill}")
            pe_fill = pg.eval_on_selector("#svPe", "el=>parseFloat(el.value)")
            ck("2c 目标PE 预填=当前pe", abs(pe_fill - q["pe"]) < 0.15, f"exp={q['pe']} fill={pe_fill}")
        else:
            print("  SKIP 2b/2c（quote 无 pe，本次不校验）")
        if pr and q.get("total_cap"):
            sh_exp = q["total_cap"] / pr
            sh_fill = pg.eval_on_selector("#svShare", "el=>parseFloat(el.value)")
            ck("2d 股本预填=total_cap/price", abs(sh_fill - sh_exp) < max(0.05, sh_exp*0.01),
               f"exp={sh_exp:.3f} fill={sh_fill}")

        # 3 计算输出不是「参数不全」
        dcf = pg.eval_on_selector("#svDcfOut", "el=>el.textContent")
        ck("3 DCF 有计算输出（预填生效）", dcf and "参数不全" not in dcf, dcf)
        dcf_num = pg.eval_on_selector("#svDcfOut", "el=>el.querySelector('b') ? el.querySelector('b').textContent : ''")
        if dcf_num:
            v = float(dcf_num.replace(",", ""))
            ck("3b DCF 输出是有限正数", 0 < v < 1e8, dcf_num)
        pe_out = pg.eval_on_selector("#svPeOut", "el=>el.textContent")
        ck("3c PE 输出有目标价", pe_out and "目标价" in pe_out, pe_out)

        # 4 改参数即时重算（事件接线用 pfx='sv'）
        pg.eval_on_selector("#svFcf", "el=>{el.value='10'; el.dispatchEvent(new Event('input',{bubbles:true}))}")
        dcf2 = pg.eval_on_selector("#svDcfOut", "el=>el.textContent")
        ck("4 改 FCF 后即时重算", dcf2 and "参数不全" not in dcf2 and dcf2 != dcf, dcf2)

        # 5 牛熊页原计算器不受影响（pfx='v' 仍在）
        pg.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
        pg.wait_for_timeout(2500)
        ck("5 牛熊页原计算器仍在", pg.locator("#vFcf").count() == 1)
        vout = pg.eval_on_selector("#vDcfOut", "el=>el.textContent")
        ck("5b 牛熊页 DCF 正常输出", vout and "参数不全" not in vout, vout)

        ck("页面无 JS 报错", len(errs) == 0, errs[:3])
        # 对计算器卡截图：modal 可能盖着（playwright 判 not visible），改用 clip 按坐标裁
        pg.eval_on_selector("#stockValCard", "el=>el.scrollIntoView({block:'center'})")
        pg.wait_for_timeout(400)
        box = pg.locator("#stockValCard").bounding_box()
        if box:
            pg.screenshot(path="shots/个股页-估值计算器.png",
                          clip={"x": box["x"], "y": box["y"],
                                "width": box["width"], "height": box["height"]})
        b.close()

    print(f"\n{len(passes)}/{len(passes)+len(fails)} PASS" +
          ("" if not fails else f"  FAIL: {fails}"))
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
