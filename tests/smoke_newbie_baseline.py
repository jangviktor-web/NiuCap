"""小白选股「随机基线回溯」端到端冒烟。

验证的是一条完整链路：/api/newbie_presets 的 retrospect 角标 →
/api/newbie_backtest 的双口径结果 → 前端渲染。

  1. 方案卡片角标：growth「可对比」，steady/hot/rebound「不可比」
  2. 点 growth：整池随机 + 同强势子池两个口径同时出现（少一个就是选择性呈现）
  3. 口径说明（caveat）必须上屏 —— 这个数字只衡量"按涨幅排序"这一个因子
  4. 每日柱状图 60 根、两根百分位游标
  5. 切到 steady：改成"无法回溯 + 原因"，且不残留上一个方案的数字
  6. 全程零 JS 报错（首页既有的 /api/screen/fields 401 属历史问题，不在此列）

前置：
  · 服务已起在 8899（`.venv/bin/python server/run.py`）
  · 需要一个能登录的测试账号（默认 bltest/bltest123，与 app.py 的 register 一致）

跑法：python3 tests/smoke_newbie_baseline.py
退出码：全过 0，有失败 1。
"""
import os
import sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("TICK_BASE", "http://127.0.0.1:8899/")
USER = os.environ.get("E2E_USER", "bltest")
PWD = os.environ.get("E2E_PWD", "bltest123")
errs, checks = [], []

def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(("  [OK] " if cond else "  [FAIL] ") + name + ((" | " + str(detail)) if detail else ""))

with sync_playwright() as p:
    # 沙箱里 playwright 版本与已下载的浏览器版本常对不上（要 1243、装的是 1208）。
    # 系统 chromium 一定在，优先用它；没有才回落到 playwright 自带浏览器。
    _exe = '/usr/bin/chromium' if os.path.exists('/usr/bin/chromium') else None
    b = p.chromium.launch(headless=True, executable_path=_exe,
                          args=['--no-sandbox', '--disable-dev-shm-usage'])
    ctx = b.new_context(viewport={'width': 1400, 'height': 1100})
    pg = ctx.new_page()
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    # 先注册（已存在则 400，忽略），再登录 —— 新环境第一次跑也能过
    pg.evaluate("""async ({u,p})=>{
        await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({username:u,password:p,display:'基线冒烟'})}).catch(()=>{});
        await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({username:u,password:p})});
      }""", {"u": USER, "p": PWD})
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(1500)
    pg.evaluate("""()=>{document.querySelectorAll('button[data-tab="newbie"]').forEach(b=>b.click())}""")
    pg.wait_for_selector('[data-nbpick="growth"]', timeout=20000)

    # 1) 卡片角标：growth 可对比、其余不可比
    badges = pg.eval_on_selector_all(
        '[data-nbpick]',
        "els=>els.map(e=>({k:e.dataset.nbpick, t:(e.querySelector('.nbbadge')||{}).textContent||''}))")
    d = {x['k']: x['t'] for x in badges}
    check("growth 卡片显示「可对比」", d.get('growth') == '可对比', d.get('growth'))
    check("steady/hot/rebound 显示「不可比」",
          all(d.get(k) == '不可比' for k in ('steady', 'hot', 'rebound')),
          {k: d.get(k) for k in ('steady', 'hot', 'rebound')})

    # 2) 点 growth → 双口径数字出现
    pg.query_selector('[data-nbpick="growth"]').click()
    pg.wait_for_timeout(6000)
    txt = pg.inner_text('#nbBaseline')
    check("出现两个口径标题", '整池随机' in txt and '同强势子池' in txt, "")
    check("两条结论都在", '弱于随机选股' in txt and '与随机选股无实质差别' in txt)
    check("口径说明（caveat）可见", '口径说明' in txt and 'turnover' in txt)
    n_track = pg.eval_on_selector_all('#nbBaseline .nbbl-track u', 'e=>e.length')
    check("两根百分位游标都渲染", n_track == 2, n_track)
    n_bar = pg.eval_on_selector_all('#nbBaseline .nbbl-mini i', 'e=>e.length')
    check("每日柱状图有 60 根", n_bar == 60, n_bar)

    # 3) 切到 steady → 换成"无法回溯 + 原因"，不能残留 growth 的数字
    pg.query_selector('[data-nbpick="steady"]').click()
    pg.wait_for_timeout(3500)
    txt2 = pg.inner_text('#nbBaseline')
    check("不可回溯方案给出原因", '无法回溯' in txt2 or '给不出' in txt2, txt2[:60].replace('\n', ' '))
    check("不可回溯时不显示百分位数字", '百分位' not in txt2)
    check("不残留上一个方案的数字", '27' not in txt2.split('\n')[0])

    print(f"console/page 错误数 = {len(errs)}")
    for e in errs[:5]:
        print("  ERR:", e)
    # 白名单：这 2 条都是"资源加载失败"类的浏览器提示，不是脚本异常：
    #   · 401 —— 首页既有的 /api/screen/fields（与本次改动无关）
    #   · 400 —— 上面"先注册再登录"里，账号已存在时 register 必然回 400
    real = [e for e in errs
            if 'Failed to load resource' not in e
            and '401 (Unauthorized)' not in e
            and '400 (Bad Request)' not in e]
    check("无 JS 报错（忽略既有的 401 资源错误）", not real, real[:3])
    b.close()

passed = sum(1 for _, ok in checks if ok)
print(f"\n{passed}/{len(checks)} PASS")
sys.exit(0 if passed == len(checks) else 1)
