#!/usr/bin/env python3.11
"""截图面板「快讯 → 板块舆情」页，证明新功能已生效（无头 chromium）。"""
import time

URL = "http://127.0.0.1:8899/"
OUT = "/workspace/sector_news_screenshot.png"

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/google-chrome",
        headless=True,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    )
    page = browser.new_page(viewport={"width": 1440, "height": 1600})
    page.goto(URL, wait_until="load", timeout=30000)
    page.wait_for_timeout(2000)

    # 0) 打开侧边菜单抽屉（默认收起）
    page.click("#navToggle", timeout=10000)
    page.wait_for_timeout(800)

    # 1) 进入「快讯」tab（抽屉里可见的那一个）
    page.locator('[data-tab="news"]').filter(visible=True).first.click(timeout=10000)
    page.wait_for_selector("#newsBody", timeout=15000)
    page.wait_for_timeout(2500)

    # 2) 切到「板块舆情」
    page.locator('[data-ntab="sector"]').filter(visible=True).first.click(timeout=10000)
    page.wait_for_function(
        "() => { var e=document.querySelector('#sectorBody');"
        "return e && (e.querySelector('.st-card') || e.innerText.length>40); }",
        timeout=25000,
    )
    page.wait_for_timeout(1500)

    page.screenshot(path=OUT, full_page=True)
    browser.close()
    print("OK 截图已保存:", OUT)
