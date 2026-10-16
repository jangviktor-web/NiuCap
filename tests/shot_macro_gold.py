import os
import sys

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "server"))
sys.path.insert(0, _ROOT)
import store  # noqa: E402

BASE = "http://127.0.0.1:8899/"
OUT_MACRO = "docs/shot-macro-gold-macro.webp"
OUT_GOLD = "docs/shot-macro-gold-gold.webp"


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
        ctx = b.new_context(viewport={"width": 1560, "height": 1400})
        ctx.add_cookies([{"name": "tick_sid", "value": tok, "url": BASE}])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(2500)

        from webp_shot import shoot

        # ---- 宏观风向卡 ----
        pg.wait_for_selector("#macroCard", timeout=60000)
        pg.wait_for_function(
            "document.querySelector('#macroCard') && "
            "document.querySelector('#macroCard').style.display !== 'none'",
            timeout=60000)
        pg.wait_for_timeout(1200)
        shoot(pg.locator("#macroCard"), path=OUT_MACRO)
        print("已保存", OUT_MACRO)

        # ---- 黄金行情卡 ----
        pg.wait_for_selector("#goldCard", timeout=60000)
        pg.wait_for_function(
            "document.querySelector('#goldCard') && "
            "document.querySelector('#goldCard').style.display !== 'none'",
            timeout=60000)
        pg.wait_for_timeout(1200)
        shoot(pg.locator("#goldCard"), path=OUT_GOLD)
        print("已保存", OUT_GOLD)

        # ---- 内容校验 ----
        macro_txt = pg.evaluate(
            "document.querySelector('#macroBody').textContent")
        gold_txt = pg.evaluate(
            "document.querySelector('#goldBody').textContent")
        print("宏观风向 KPIs:", [k for k in ("降息概率", "实际利率", "期限利差", "恐慌指数")
                                if k in macro_txt])
        print("黄金行情行数:", gold_txt.count("积存金") + gold_txt.count("AU9999")
              + gold_txt.count("AUTD") + gold_txt.count("伦敦金现"))
        print("JS 错误:", errs if errs else "无")

        assert not errs, f"页面 JS 错误: {errs}"
        assert "降息概率" in macro_txt and "恐慌指数" in macro_txt
        assert "AU9999" in gold_txt and "AUTD" in gold_txt
        print("全部断言通过")


if __name__ == "__main__":
    main()
