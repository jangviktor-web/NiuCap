#!/usr/bin/env python3
# Task#99 验证：命令面板(Cmd+K) + 沪深交易时段时钟
# 纯前端增强，零后端改动；浏览器端到端验证。
import os, re, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
SHOT = "shots_cmd_clock"
os.makedirs(SHOT, exist_ok=True)

results, errors = [], []
def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("✅" if ok else "❌"), name, ("" if ok else "→ " + detail))

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()
        errs = []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(800)

        # ---- 1. Cmd+K 打开面板 ----
        page.keyboard.press("Control+k")
        page.wait_for_timeout(300)
        opened = page.evaluate("() => !document.getElementById('cmdPalette').hidden")
        rec("Ctrl+K 打开命令面板", opened)
        focused = page.evaluate("() => document.activeElement && document.activeElement.id === 'cmdInput'")
        rec("打开后输入聚焦", focused)

        # ---- 2. 跳页面：输入「个股」过滤 → 选个股分析 ----
        page.fill("#cmdInput", "个股")
        page.wait_for_function(
            "() => { var n=document.querySelectorAll('#cmdList .cmdp-item .mc-name');"
            " return Array.from(n).some(x => x.textContent.indexOf('个股分析')>=0); }", timeout=5000)
        names = page.evaluate("[...document.querySelectorAll('#cmdList .cmdp-item .mc-name')].map(x=>x.textContent)")
        rec("过滤出「个股分析」候选", any('个股分析' in n for n in names), str(names))
        page.keyboard.press("Enter")
        try:
            page.wait_for_selector("#tab-stock.on", timeout=5000)
            rec("Enter 跳转到个股分析页", True)
        except Exception as e:
            rec("Enter 跳转到个股分析页", False, str(e))
        on_tab = page.evaluate("() => document.querySelector('.tabs button[data-tab=stock]').classList.contains('on')")
        rec("个股 tab 高亮", on_tab)
        page.screenshot(path=f"{SHOT}/cmd_goto_stock.png")

        # ---- 3. 搜股票：Ctrl+K → 600519 → 跳个股页 ----
        page.keyboard.press("Control+k")
        page.wait_for_timeout(300)
        reopened = page.evaluate("() => !document.getElementById('cmdPalette').hidden")
        rec("再次 Ctrl+K 重新打开", reopened)
        page.fill("#cmdInput", "600519")
        page.wait_for_function(
            "() => { var s=document.querySelectorAll('#cmdList .cmdp-item .mc-sub');"
            " return Array.from(s).some(x => x.textContent.indexOf('sh600519')>=0); }", timeout=8000)
        subs = page.evaluate("[...document.querySelectorAll('#cmdList .cmdp-item .mc-sub')].map(x=>x.textContent)")
        rec("搜索出 sh600519 候选", any('sh600519' in s for s in subs), str(subs))
        page.keyboard.press("Enter")
        try:
            page.wait_for_function("() => window.state && state.stockCode === 'sh600519'", timeout=8000)
            rec("Enter 选中股票 → 跳个股页(stockCode=sh600519)", True)
        except Exception as e:
            code = page.evaluate("() => window.state && state.stockCode")
            rec("Enter 选中股票 → 跳个股页(stockCode=sh600519)", False, f"{e} | code={code}")
        page.screenshot(path=f"{SHOT}/cmd_goto_stockcode.png")

        # ---- 4. Esc 关闭 ----
        page.keyboard.press("Control+k")  # 若已关则打开；下面用 Esc 关
        page.wait_for_timeout(200)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        closed = page.evaluate("() => document.getElementById('cmdPalette').hidden")
        rec("Esc 关闭面板", closed)

        # ---- 5. 交易时段时钟 ----
        clk = page.evaluate("() => { var e=document.getElementById('mktClock'); return e? e.textContent : null; }")
        ok_clk = bool(clk) and bool(re.search(r"(交易中|午休|集合竞价|已收盘|休市|盘后)", clk or ""))
        rec("交易时段时钟有状态文本", ok_clk, str(clk))
        cls = page.evaluate("() => { var e=document.getElementById('mktClock'); return e? e.className : ''; }")
        rec("时钟带状态类名(mc-*)", 'mc-' in cls, cls)
        page.screenshot(path=f"{SHOT}/clock_nav.png", full_page=False)

        # ---- 6. 控制台无错误 ----
        rec("页面无 JS 报错", not errs, "; ".join(errs[:3])[:200])

        browser.close()

    total = len(results); passed = sum(1 for _,ok,_ in results if ok)
    print(f"\n通过 {passed}/{total}")
    if passed != total:
        sys.exit(1)

if __name__ == "__main__":
    main()
