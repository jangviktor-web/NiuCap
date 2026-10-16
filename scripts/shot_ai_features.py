#!/usr/bin/env python3.11
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from webp_shot import shoot
# 为本轮两个 AI 功能截图：
#   ① 策略回测页「✨AI 增强解析」开关 + 解析结果 AI 角标（需登录 nlpick）
#   ② 个股页「✨AI 深度分析」卡片（LLM 解读）
# 注意：#190 改版后桌面端也用「菜单」抽屉导航（.tabs 横排已隐藏），
# 切 tab 走「点 #navToggle → 点抽屉里 .nd-item[data-tab]」。
# 用法：export E2E_USER=xxx E2E_PWD=xxx（普通账号即可，不存在会自动注册）
import os
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8899/"
USER = os.environ.get("E2E_USER", "")
PWD = os.environ.get("E2E_PWD", "")
OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shots"))
os.makedirs(OUT, exist_ok=True)

if not USER or not PWD:
    print("请先设置环境变量 E2E_USER / E2E_PWD（普通账号即可，不存在会自动注册）")
    sys.exit(2)

# 口语化复杂句：规则版全 None，AI 增强才能拆出（端到端已验证）
NL_TEXT = "股价从布林带下轨反弹而且MACD出现底背离的时候轻仓介入，赚一成五就收工，跌7%认错离场"


def open_tab(pg, tab):
    pg.click("#navToggle")
    pg.wait_for_timeout(300)
    pg.click(f'.nd-item[data-tab="{tab}"]')
    pg.wait_for_timeout(1500)


def shot(pg, name):
    # 中性化顶栏用户区：截图不暴露测试账号名（项目惯例，同 #185 清理）
    pg.evaluate("""() => {
      const u = document.querySelector('#uArea');
      if (u) u.innerHTML = '<button class="ubtn">已登录</button>';
    }""")
    path = os.path.join(OUT, name + ".png")
    shoot(pg, path=path)
    print("saved", path, os.path.getsize(path), "bytes")


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)

    # 登录（page.request 与页面共享 cookie 罐）；账号不存在时先注册
    r = pg.request.post(BASE + "api/auth/register",
                        data={"username": USER, "password": PWD, "display": USER})
    if r.status_code >= 400:
        r = pg.request.post(BASE + "api/auth/login",
                            data={"username": USER, "password": PWD})
    print("login:", r.status)
    assert r.json().get("ok"), "login failed"

    pg.goto(BASE, wait_until="networkidle")
    pg.wait_for_timeout(2500)

    # ---------- ① 策略解析 AI 增强 ----------
    open_tab(pg, "backtest")
    pg.check("#nlLLM")
    pg.fill("#nlText", NL_TEXT)
    pg.click("#nlParse")
    # 等解析结果区出现 AI 角标（LLM 调用有耗时，最多等 30s）
    pg.wait_for_selector("#nlResult:has-text('✨AI增强')", timeout=30000)
    pg.wait_for_timeout(800)
    pg.eval_on_selector("#nlText", "el => el.scrollIntoView({block:'center'})")
    pg.wait_for_timeout(400)
    shot(pg, "shot-intent-llm")

    # ---------- ② 个股 AI 深度分析 ----------
    open_tab(pg, "rank")
    pg.wait_for_selector("#rankBody tbody tr[data-code]", timeout=10000)
    pg.click("#rankBody tbody tr[data-code]:first-child")
    pg.wait_for_timeout(4000)
    # 等 AI 卡片按钮出现（appendStockAiCard 在 renderStock 后追加）
    pg.wait_for_selector("#stockAiBtn", timeout=10000)
    pg.click("#stockAiBtn")
    # 等分析结果：最终渲染必含 .intentbox（初始空态没有）
    pg.wait_for_selector("#stockAiOut .intentbox", timeout=90000)
    pg.wait_for_timeout(800)
    pg.eval_on_selector("#stockAiBtn", "el => el.scrollIntoView({block:'center'})")
    pg.wait_for_timeout(400)
    shot(pg, "shot-stock-ai")

    b.close()
print("DONE")
