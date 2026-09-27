"""方案 A/B 交付截图：策略体检面板（15 行 + 经典指标类）与模拟净值（新质量指标）。

用法：python3 tests/shot_plan_a.py
前置：服务在 8899 且 40 天体检已预热（e2e 跑过即满足）。
"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT_EVAL = "docs/shot-strategy-eval.png"
OUT_SIM = "docs/shot-sim.png"


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1500, "height": 1100})
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # ---------- 策略体检 ----------
        pg.click('button[data-tab="strategy"]')
        pg.wait_for_timeout(1200)
        pg.evaluate("""() => {
          var s = document.getElementById('evalDays');
          if (s) s.value = '40';
        }""")
        pg.click("#evalBtn")
        pg.wait_for_selector("#evalBody table.evt", timeout=180000)
        pg.wait_for_timeout(2000)
        # 截体检面板（含 15 行 + 经典指标分组 + 分层测试 + 跳过说明）
        pg.locator("#evalPanel").screenshot(path=OUT_EVAL)
        print("已保存", OUT_EVAL)

        # ---------- 模拟净值 ----------
        pg.wait_for_selector("#simBox", state="visible", timeout=8000)
        pg.select_option("#simKey", "oversold_rebound")
        pg.click("#simRun")
        pg.wait_for_selector("#simBody svg", timeout=15000)
        pg.wait_for_timeout(1000)
        pg.locator("#simBox").screenshot(path=OUT_SIM)
        print("已保存", OUT_SIM)

        b.close()


if __name__ == "__main__":
    main()
