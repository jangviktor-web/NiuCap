"""#88 双源快讯流 交付截图：快讯页签（红条 + 来源徽章 + 情绪点）。

用法：python3 tests/shot_newsfeed.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import store  # noqa: E402

BASE = "http://127.0.0.1:8899/"
OUT = "docs/shot-newsfeed.png"


def main():
    adm = next((u for u in store.list_users()
                if (u.get("username") or "").lower() == "admin"), None)
    tok = store.create_session(adm["id"]) if adm else ""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1560, "height": 1400})
        if tok:
            ctx.add_cookies([{"name": "tick_sid", "value": tok, "url": BASE}])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1200)
        pg.evaluate("syncTabs('news'); loadNews()")
        pg.wait_for_timeout(6000)
        pg.locator("#tab-news .card").first.screenshot(path=OUT)
        print("已保存", OUT, "· JS 错误", len(errs))
        b.close()


if __name__ == "__main__":
    main()
