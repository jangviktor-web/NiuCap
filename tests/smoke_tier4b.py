from playwright.sync_api import sync_playwright
import sys

BASE = "http://127.0.0.1:8899"
# 选股端点（含 rating_batch）已要求登录，本脚本先拿会话再跑。
# 账号由 ensure_login() 幂等注册/登录，跑多少次都不会炸。
USER = "e2e_screen"
PWD = "E2eScreen123456"
errors = []


def ensure_login(pg):
    """登录并把页面里的 authState 同步过来（guardScreen 依赖它判态）。"""
    pg.evaluate(
        """(function(){
             return fetch('/api/auth/login', {
               method:'POST', headers:{'Content-Type':'application/json'},
               body: JSON.stringify({username: '%s', password: '%s'})
             }).then(function(r){ return r.json(); })
               .then(function(j){ if(!j.ok) throw new Error(j.detail||'登录失败');
                                  return loadMe(); });
           })()""" % (USER, PWD)
    )
    pg.wait_for_function("!!authState.user", timeout=20000)

def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1280, "height": 1000})
        def on_console(m):
            if m.type != "error":
                return
            # 页面首次加载（未登录）时 /api/screen/fields 会被守卫回 401，
            # 浏览器必记一条 console error。它是「资源没取到」而非 JS 异常，
            # 理由在登录守卫里 —— 混进断言会让这里永远红。
            if "status of 401" in m.text:
                return
            errors.append(m.text)

        pg.on("console", on_console)
        pg.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))
        pg.goto(BASE + "/", wait_until="load", timeout=20000)
        ensure_login(pg)

        # ---------- 方案 A：对比页 ----------
        pg.evaluate("""
        (function(){
          function actTab(x){
            document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
            document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
            document.querySelectorAll('button[data-tab="'+x+'"]').forEach(b=>b.classList.add('on'));
            var tab=document.querySelector('#tab-'+x); if(tab) tab.classList.add('on');
          }
          actTab('compare');
          document.querySelector('#cmpCodes').value='600519,000858';
          runCompare();
        })();
        """)
        pg.wait_for_selector(".cmptbl .rtag", timeout=30000)
        cmp_badges = pg.locator(".cmptbl .rtag").count()
        meta_txt = pg.locator(".card-h:has-text('强弱速览') .meta").inner_text()
        print("[A] 对比页评级徽标数:", cmp_badges)
        print("[A] 强弱速览含指标评级:", "指标评级" in meta_txt, "|", meta_txt[:90])

        # ---------- 方案 B1：选股列表按需按钮 ----------
        pg.evaluate("""
        (function(){
          function actTab(x){
            document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
            document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
            document.querySelectorAll('button[data-tab="'+x+'"]').forEach(b=>b.classList.add('on'));
            var tab=document.querySelector('#tab-'+x); if(tab) tab.classList.add('on');
          }
          actTab('screen');
          runScreen();
        })();
        """)
        pg.wait_for_selector("#screenBody table", timeout=30000)
        before = pg.locator("#screenBody .rtag").count()
        pg.evaluate("loadScreenRatings();")
        pg.wait_for_function(
            "document.querySelectorAll('#screenBody .rtag').length > 0",
            timeout=60000,
        )
        after = pg.locator("#screenBody .rtag").count()
        print("[B1] 点击前徽标:", before, "| 点击后徽标:", after)

        b.close()

    print("JS_ERRORS:", len(errors))
    for e in errors[:10]:
        print("  -", e[:200])
    ok = (cmp_badges >= 1) and (after >= 1) and (len(errors) == 0)
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
