import os
import sys

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "server"))
sys.path.insert(0, _ROOT)
import store  # noqa: E402

BASE = "http://127.0.0.1:8899/"
OUT1 = "docs/shot-phase-live.webp"
OUT2 = "docs/shot-breadth-live.webp"


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

        # ---- 市场情绪周期（含盘中实时连板梯队）在 #tempCard 内 ----
        pg.wait_for_selector("#phaseBlock", timeout=60000)
        pg.wait_for_timeout(3000)          # 等 phaseBlock 异步填充
        from webp_shot import shoot
        shoot(pg.locator("#tempCard"), path=OUT1)
        print("已保存", OUT1)

        # ---- 市场宽度（含今日实时广度） ----
        pg.wait_for_selector("#breadthBody", timeout=60000)
        pg.wait_for_timeout(2000)
        # 宽度卡片很高（三张图），只截上部：实时广度 + 摘要 + 第一张图
        from webp_shot import shoot
        box = pg.locator("#breadthCard").bounding_box()
        shoot(pg, path=OUT2, clip={"x": box["x"], "y": box["y"],
                                   "width": box["width"], "height": 620})
        print("已保存", OUT2)

        # 校验实时块确实渲染
        has_live_phase = pg.evaluate(
            "!!document.querySelector('#tempCard') && document.querySelector('#tempCard').textContent.includes('实时连板梯队')")
        has_live_breadth = pg.evaluate(
            "!!document.querySelector('#breadthBody') && document.querySelector('#breadthBody').textContent.includes('今日实时广度')")
        print("实时连板梯队渲染:", has_live_phase)
        print("今日实时广度渲染:", has_live_breadth)

        print("页面 JS 错误:", len(errs), errs[:3])
        b.close()


if __name__ == "__main__":
    main()
