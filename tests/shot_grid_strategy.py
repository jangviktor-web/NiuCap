"""网格策略扩展截图：回测面板（策略下拉 + 移动网格结果 + K线买卖点 + 档位表）。

用法：python3 tests/shot_grid_strategy.py
前置：服务在 8899（网格回测不要求登录）。
"""
from playwright.sync_api import sync_playwright

OUT = "docs/shot-grid-strategy.png"


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1500, "height": 2200})
        pg.goto("http://127.0.0.1:8899/", wait_until="networkidle")
        pg.wait_for_timeout(1200)

        pg.click('button[data-tab="backtest"]')
        pg.wait_for_timeout(500)
        pg.fill("#grCode", "sh510300")
        pg.fill("#grStep", "2")
        pg.fill("#grBand", "20")
        pg.select_option("#grStrategy", "moving")   # 移动网格（会显示平移次数）
        pg.click("#grRun")
        pg.wait_for_timeout(5000)
        pg.locator("#grOutput").screenshot(path=OUT)
        print("已保存", OUT)
        b.close()


if __name__ == "__main__":
    main()
