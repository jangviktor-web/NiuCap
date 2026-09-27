"""方案 D 交付截图：策略体检表（含「市况敏感度」列）与市场宽度卡片（含「市况读数」+ Chop 震荡指数折线）。

用法：python3 tests/shot_plan_d.py
前置：服务在 8899。体检表用 40 天窗口（约 80s，已验证可返回）；
      宽度卡片为 API 直取（已按数据版本缓存，秒级）。
"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT_EVAL = "docs/shot-strategy-eval-d.png"
OUT_BREADTH = "docs/shot-breadth-d.png"
EVAL_DAYS = "40"  # 近 40 天亦为纯震荡市（0 趋势日），足以展示市况列


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1560, "height": 1500})
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # ---------- 策略体检（含方案 D 市况敏感度列） ----------
        pg.click('button[data-tab="strategy"]')
        pg.wait_for_timeout(1200)
        pg.evaluate("""() => {
          var s = document.getElementById('evalDays');
          if (s) s.value = '%s';
        }""" % EVAL_DAYS)
        pg.click("#evalBtn")
        # 体检表（15 行 + 市况敏感度列）
        pg.wait_for_selector("#evalBody table.evt", timeout=180000)
        pg.wait_for_timeout(2500)
        # 确认市况敏感度列已渲染（方案 D 标记）
        pg.wait_for_function(
            """() => {
              var ths = Array.from(document.querySelectorAll('#evalBody th'));
              return ths.some(t => t.textContent.indexOf('市况敏感度') >= 0);
            }""",
            timeout=10000,
        )
        pg.locator("#evalPanel").screenshot(path=OUT_EVAL)
        print("已保存", OUT_EVAL)

        # ---------- 市场宽度（含方案 D 市况读数 + Chop 折线） ----------
        pg.click('button[data-tab="rank"]')
        pg.wait_for_selector("#breadthCard", state="visible", timeout=15000)
        pg.wait_for_selector("#breadthBody svg", timeout=20000)
        pg.wait_for_function(
            """() => {
              var txt = document.getElementById('breadthBody').innerText || '';
              return txt.indexOf('市况') >= 0 && txt.indexOf('Chop') >= 0;
            }""",
            timeout=15000,
        )
        pg.wait_for_timeout(800)
        pg.locator("#breadthCard").screenshot(path=OUT_BREADTH)
        print("已保存", OUT_BREADTH)

        b.close()


if __name__ == "__main__":
    main()
