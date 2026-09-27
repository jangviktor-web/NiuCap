#!/usr/bin/env python3.11
# 为 README 生成功能截图：逐 tab 打开并截取视口图。
import os, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8899/"
OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "screenshots")
OUT = os.path.abspath(OUT)
os.makedirs(OUT, exist_ok=True)

# (tab名, 文件名, 描述)
TABS = [
    ("rank",     "01-dashboard",  "市场榜单"),
    ("newbie",   "02-newbie",     "小白选股"),
    ("strategy", "03-strategy",   "策略选股"),
    ("screen",   "04-screen",     "条件选股"),
    ("news",     "05-news",       "双源快讯"),
    ("backtest", "06-backtest",   "策略回测"),
    ("about",    "07-about",      "关于本面板"),
]

def shot(page, name):
    path = os.path.join(OUT, name + ".png")
    page.screenshot(path=path)
    print("saved", path, os.path.getsize(path), "bytes")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    pg.goto(BASE, wait_until="networkidle")
    pg.wait_for_timeout(2500)

    # 1) 市场榜单（默认即开）
    shot(pg, "01-dashboard")

    # 2) 钻取个股分析：点击榜单首行
    try:
        pg.wait_for_selector("#rankBody tr", timeout=8000)
        pg.click("#rankBody tr:first-child")
        pg.wait_for_timeout(3500)
        shot(pg, "08-stock")
    except Exception as e:
        print("stock drill failed:", e)

    # 3) 其余 tab
    for tab, name, _ in TABS[1:]:
        try:
            pg.click(f'button[data-tab="{tab}"]', timeout=5000)
            pg.wait_for_timeout(2800)
            shot(pg, name)
        except Exception as e:
            print(f"tab {tab} failed:", e)

    b.close()
print("DONE")
