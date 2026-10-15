"""选股登录守卫：截图留档。

产出：
  shots/screen_guard_login.png   未登录点「执行选股」→ 弹登录框 + 守卫提示条
  shots/screen_guard_result.png  登录后自动续跑出的选股结果

用法：export E2E_USER=xxx E2E_PWD=xxx（普通账号即可，不存在会自动注册）
      python3 tests/shot_screen_guard.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
USER = os.environ.get("E2E_USER", "")
PWD = os.environ.get("E2E_PWD", "")
OUT = os.path.join(os.path.dirname(__file__), "..", "shots")
OUT = os.path.abspath(OUT)

if not USER or not PWD:
    print("请先设置环境变量 E2E_USER / E2E_PWD（普通账号即可，不存在会自动注册）")
    sys.exit(2)


def act_tab(pg, tab):
    pg.evaluate(
        """(function(x){
          document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
          document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
          document.querySelectorAll('button[data-tab="'+x+'"]').forEach(b=>b.classList.add('on'));
          var tab=document.querySelector('#tab-'+x); if(tab) tab.classList.add('on');
        })('%s')""" % tab
    )


def mask_names(pg):
    """截图前把顶栏真实用户名中性化（#185 惯例，不暴露测试账号名）。

    登录框若仍在（失败态留档），输入框里的账号名一并掩掉。"""
    pg.evaluate(
        """() => { let un = document.querySelector('#uArea .uname b');
          if(un) un.textContent = '演示用户';
          let au = document.getElementById('authUser');
          if(au) au.value = 'demo_user'; }""")


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
        # 账号不存在先注册（与 shot_ai_features 同策略；已存在则忽略失败直接登录）
        r = pg.request.post(BASE + "/api/auth/register",
                            data={"username": USER, "password": PWD, "display": USER})
        print("注册/已存在:", r.status)
        pg.fill("#authUser", USER)
        pg.fill("#authPwd", PWD)
        pg.click("#authSubmit")
        # 登录成功的铁证是弹框关闭（.on class 被移除；注意关闭态是 display:none，
        # 不能用 wait_for_selector 默认的可见等待）；此前不能截图中态
        pg.wait_for_function(
            "() => !document.getElementById('authModal').classList.contains('on')",
            timeout=20000)
        # 再等选股续跑出终态。注意：初始 .empty 文案「设置条件后点击执行选股」
        # 一直在 DOM 里，不能当终态信号；只认 table 或明确的结局文案
        pg.wait_for_function(
            """() => { let el = document.getElementById('screenBody');
              if(!el) return false;
              if(el.querySelector('table')) return true;
              let t = el.textContent || '';
              return t.indexOf('没有符合条件') >= 0 || t.indexOf('筛选失败') >= 0; }""",
            timeout=90000)
        pg.wait_for_timeout(800)
        mask_names(pg)
        f2 = os.path.join(OUT, "screen_guard_result.png")
        pg.screenshot(path=f2)
        print("已截图:", f2)

        b.close()


if __name__ == "__main__":
    main()
