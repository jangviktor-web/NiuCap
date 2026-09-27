"""关于页交付截图：验证指标/策略计数、模块构成、后台清单、版本构建均为动态渲染。

用法：python3 tests/shot_about.py
前置：服务在 8899。关于页公开，无需登录。
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT = "docs/shot-about.png"


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1560, "height": 1600})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1200)

        # 切到关于页并等双接口（health + about）渲染完
        pg.evaluate("syncTabs('about')")
        pg.wait_for_timeout(2500)

        txt = pg.inner_text("#aboutBody")
        for kw in ["模块构成", "61", "29", "版本与构建", "v1.2.6", "体检缓存", "系统备份"]:
            print(("OK  " if kw in txt else "MISS") + " " + kw)
        print("字数:", len(txt))

        # 截关于卡片
        node = pg.locator("#tab-about .card")
        node.screenshot(path=OUT)
        print("截图:", OUT)

        print("JS 错误:", errs if errs else "无")
        b.close()


if __name__ == "__main__":
    main()
