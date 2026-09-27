"""龙虎榜后验字段（东财源）交付截图。

两张图：
  1. 最新榜（2026-09-24）：上榜原因列点亮（机构家数+成功率），D+1/D+2/D+5/D+10
     因尚未到期显示 —（体现后验语义）
  2. 历史榜（2026-09-18，用 evaluate 注入数据重渲染）：D+1/D+2 有值，
     红涨绿跌着色（体现后验增强的完整价值）

用法：python3 tests/shot_lhb_em.py
前置：服务在 8899；需 admin 账号。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import store  # noqa: E402

BASE = "http://127.0.0.1:8899/"
OUT_LATEST = "docs/shot-lhb-em-latest.png"
OUT_HIST = "docs/shot-lhb-em-history.png"


def admin_token():
    adm = next((u for u in store.list_users()
                if (u.get("username") or "").lower() == "admin"), None)
    if not adm:
        raise SystemExit("需要 admin 账号")
    return store.create_session(adm["id"])


def fetch(path):
    with urllib.request.urlopen(BASE.rstrip("/") + path, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    tok = admin_token()
    hist = fetch("/api/lhb?date=2026-09-18&limit=30")
    print("历史榜(09-18) 条数:", len(hist.get("items") or []),
          "D1有值:", sum(1 for x in hist["items"] if x.get("d1") is not None))

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1560, "height": 1400})
        ctx.add_cookies([{"name": "tick_sid", "value": tok, "url": BASE}])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # 切到 市场榜单 -> 龙虎榜
        pg.evaluate("showBoard('hot:lhb')")
        pg.wait_for_timeout(2500)
        pg.locator("#hotCard").screenshot(path=OUT_LATEST)
        print("已保存", OUT_LATEST)

        # 注入 09-18 历史榜数据重渲染（D+1/D+2 有值）
        pg.evaluate("""async (data) => {
            hotState.data.lhb = data;
            renderHot();
        }""", hist)
        pg.wait_for_timeout(800)
        pg.locator("#hotCard").screenshot(path=OUT_HIST)
        print("已保存", OUT_HIST)

        print("页面 JS 错误:", len(errs), errs[:3])
        b.close()


if __name__ == "__main__":
    main()
