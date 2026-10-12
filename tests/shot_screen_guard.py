"""选股登录守卫：截图留档。

产出：
  shots/screen_guard_login.png   未登录点「执行选股」→ 弹登录框 + 守卫提示条
  shots/screen_guard_result.png  登录后自动续跑出的选股结果

用法：python3 tests/shot_screen_guard.py
"""
import os

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
USER = "e2e_screen"
PWD = "E2eScreen123456"
OUT = os.path.join(os.path.dirname(__file__), "..", "shots")
OUT = os.path.abspath(OUT)


def act_tab(pg, tab):
    pg.evaluate(
        """(function(x){
          document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
          document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
          document.querySelectorAll('button[data-tab="'+x+'"]').forEach(b=>b.classList.add('on'));
          var tab=document.querySelector('#tab-'+x); if(tab) tab.classList.add('on');
        })('%s')""" % tab
    )


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1366, "height": 1000})
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        pg.wait_for_function("typeof authState!=='undefined' && authState.loaded",
                             timeout=15000)

        # ---- 1. 未登录点「执行选股」→ 登录框 ----
        act_tab(pg, "screen")
        pg.evaluate("document.querySelector('#f_run').click();")
        pg.wait_for_selector("#authModal.on", timeout=8000)
        pg.wait_for_timeout(400)
        f1 = os.path.join(OUT, "screen_guard_login.png")
        pg.screenshot(path=f1)
        print("已截图:", f1)

        # ---- 2. 登录 → 自动续跑出结果 ----
        pg.fill("#authUser", USER)
        pg.fill("#authPwd", PWD)
        pg.click("#authSubmit")
        pg.wait_for_selector("#screenBody table", state="attached", timeout=60000)
        pg.wait_for_timeout(800)
        f2 = os.path.join(OUT, "screen_guard_result.png")
        pg.screenshot(path=f2)
        print("已截图:", f2)

        b.close()


if __name__ == "__main__":
    main()
