"""#87 同步守门 + #89 词典情绪 交付截图。

一张图：五粮液(000858) 个股页公告区——「集团增持公司股票」公告
带 ▲看涨 +2.0 徽章（词典正词「增持」权重 2.0），程序性公告标 ─中性。
CLI 守门输出（守门三重判断 + 增量预筛）是终端文本，由自检脚本覆盖。

用法：python3 tests/shot_gate_sentiment.py
前置：服务在 8899。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import store  # noqa: E402

BASE = "http://127.0.0.1:8899/"
OUT = "docs/shot-sentiment-badge.png"


def admin_token():
    adm = next((u for u in store.list_users()
                if (u.get("username") or "").lower() == "admin"), None)
    if not adm:
        raise SystemExit("需要 admin 账号")
    return store.create_session(adm["id"])


def main():
    tok = admin_token()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1560, "height": 1500})
        ctx.add_cookies([{"name": "tick_sid", "value": tok, "url": BASE}])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # 五粮液 000858：有「集团增持公司股票」公告（看涨 +2.0）
        pg.evaluate("openStock('sz000858')")
        pg.wait_for_timeout(9000)           # 等扩展数据（含公告+情绪分）加载
        pg.locator("#stockExtra").screenshot(path=OUT)
        print("已保存", OUT)

        print("页面 JS 错误:", len(errs), errs[:3])
        b.close()


if __name__ == "__main__":
    main()
