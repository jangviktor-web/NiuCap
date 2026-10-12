"""Tier 4 前端冒烟：打开个股 → 指标多空评级徽标渲染 → 零 console 报错。"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
CODE = "600000"


def main():
    errors = []
    rtags = 0
    pill_text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="load", timeout=20000)
        # 调用 SPA 全局函数打开个股（绕过导航抖动）
        page.evaluate(f"openStock('{CODE}')")
        try:
            page.wait_for_selector(".rtag", timeout=20000)
        except Exception as e:
            print("[FAIL] 未等到评级徽标 .rtag 出现:", e)
            browser.close()
            sys.exit(1)
        rtags = page.eval_on_selector_all(".rtag", "els => els.length")
        # 综合评级 pill 文本
        pill_text = page.evaluate(
            "() => { const el=[...document.querySelectorAll('.meta')].find(e=>e.textContent.includes('指标评级')); return el?el.textContent.trim():''; }"
        )
        browser.close()

    ok = rtags > 0 and "指标评级" in pill_text and not errors
    print(f"评级徽标数 .rtag = {rtags}")
    print(f"综合评级 pill 文本 = {pill_text}")
    print(f"console/page 错误数 = {len(errors)}")
    if errors:
        for e in errors[:10]:
            print("  ERR:", e)
    if ok:
        print("[OK] Tier4 前端渲染正常，零 JS 报错")
        sys.exit(0)
    else:
        print("[FAIL] Tier4 前端冒烟未通过")
        sys.exit(1)


if __name__ == "__main__":
    main()
