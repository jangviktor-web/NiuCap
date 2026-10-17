import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = "shots"

def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1560, "height": 1700})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console:"+m.text) if m.type=="error" else None)

        # 1) 首页/行情
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)
        pg.screenshot(path=f"{OUT}/shot-theme-home.png", full_page=False)
        print("home OK")

        # 2) 个股页（含 ECharts 行情/指标图）
        try:
            pg.evaluate("openStock('sh600519')")
            pg.wait_for_timeout(4000)
            stock = pg.locator("#stockWrap")
            if stock.count():
                stock.screenshot(path=f"{OUT}/shot-theme-stock.png")
            else:
                pg.screenshot(path=f"{OUT}/shot-theme-stock.png")
            print("stock OK")
        except Exception as e:
            print("stock ERR", e)

        # 3) 关于页
        try:
            pg.evaluate("syncTabs('about')")
            pg.wait_for_timeout(2500)
            node = pg.locator("#tab-about .card")
            if node.count():
                node.screenshot(path=f"{OUT}/shot-theme-about.png")
            else:
                pg.screenshot(path=f"{OUT}/shot-theme-about.png")
            print("about OK")
        except Exception as e:
            print("about ERR", e)

        # 关键颜色自检
        def has(sel, prop):
            try:
                return pg.evaluate(f"(()=>{{const e=document.querySelector({sel!r});return e?getComputedStyle(e).{prop}:'NO_EL'}})()")
            except Exception as e:
                return "ERR:"+str(e)
        print("body bg   =", has("body", "backgroundColor"))
        print("header bg =", has("header", "backgroundColor"))
        print("card bg   =", has(".card", "backgroundColor"))
        print("accent btn=", has(".btn.pri", "backgroundColor"))
        print("up color  =", has(".up", "color"))
        print("JS 错误   =", errs[:8] if errs else "无")

        b.close()

if __name__ == "__main__":
    main()
