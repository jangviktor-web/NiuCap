"""后台管理新增能力交付截图。

两张图：
  1. 「🎯 体检缓存」面板：当前缓存占用（哪组参数 / 覆盖多少策略 / 何时算的）
     + 预热 120/40、清空缓存按钮。等 120 天预热完成再拍，让面板是填充态。
  2. 「💾 系统备份」面板：立即备份按钮 + 已有备份清单。先触发一次备份让清单非空。

用法：python3 tests/shot_admin.py
前置：服务在 8899。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import store  # noqa: E402

BASE = "http://127.0.0.1:8899/"
OUT_CACHE = "docs/shot-admin-evalcache.png"
OUT_BACKUP = "docs/shot-admin-backup.png"


def admin_token():
    adm = next((u for u in store.list_users()
               if (u.get("username") or "").lower() == "admin"), None)
    if not adm:
        raise SystemExit("需要 admin 账号")
    return store.create_session(adm["id"])


def api(path, method="GET", body=None, token=""):
    req = urllib.request.Request(
        BASE.rstrip("/") + path,
        data=(json.dumps(body).encode() if body is not None else None),
        method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Cookie", f"tick_sid={token}")
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.loads(r.read().decode())


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

        # 等 120 天预热完成（让体检缓存面板是填充态，截图才有信息量）
        print("等待 120 天体检预热完成…")
        for _ in range(100):
            try:
                st = api("/api/admin/eval_cache", token=tok)
                if st.get("ready") and st.get("days") == 120:
                    print("  缓存已就绪:", st.get("n_strategies"), "策略 ·", st.get("cost_seconds"), "s")
                    break
            except Exception:
                pass
            time.sleep(3)

        # 进入后台管理（probeAdmin 偶尔在 Playwright 下没及时翻可见，直接强制）
        pg.evaluate("showAdminTab()")
        pg.wait_for_timeout(300)
        pg.evaluate("syncTabs('admin')")
        pg.wait_for_timeout(800)

        # ---- 体检缓存面板 ----
        pg.click('button[data-adm="evalcache"]')
        pg.wait_for_timeout(1000)
        pg.locator("#admBody").screenshot(path=OUT_CACHE)
        print("已保存", OUT_CACHE)

        # ---- 系统备份面板：先触发一次备份让清单非空 ----
        try:
            rb = api("/api/admin/backup", method="POST", body={}, token=tok)
            print("  已触发备份:", rb.get("name"))
        except Exception as e:
            print("  备份触发失败:", e)
        pg.click('button[data-adm="backup"]')
        pg.wait_for_timeout(1200)
        pg.locator("#admBody").screenshot(path=OUT_BACKUP)
        print("已保存", OUT_BACKUP)

        # 删掉截图用的那份备份，避免 backups/ 堆积
        try:
            bn = rb.get("path")
            if bn and os.path.exists(bn):
                os.remove(bn)
        except Exception:
            pass

        print("JS 错误:", errs if errs else "无")
        b.close()


if __name__ == "__main__":
    main()
