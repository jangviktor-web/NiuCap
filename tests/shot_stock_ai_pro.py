"""A/C/B 个股页 AI 深度分析增强：截图验证（大白话块 / 资金筹码行 / 缓存提示）。

用 admin 会话注入 cookie，打开个股页，等 AI 卡渲染后截 #stockAiCard。
系统 python3 有 Chromium（.venv 没有）。
"""
import sys, os, time
sys.path.insert(0, 'server'); sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
import webp_shot

BASE = 'http://127.0.0.1:8899/'
# 优先用环境变量传入的 session（服务预热期直连库建 session 会 database is locked）；
# 未提供时退回 store.create_session。
tok = os.environ.get('TICK_SID')
if not tok:
    import store
    adm = next(u for u in store.list_users() if (u.get('username') or '').lower() == 'admin')
    tok = store.create_session(adm['id'])
out_webp = 'docs/shot-stock-ai-pro.webp'
out_txt = 'docs/shot-stock-ai-pro.txt'

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width': 1500, 'height': 1400}, device_scale_factor=2)
    ctx.add_cookies([{'name': 'tick_sid', 'value': tok, 'url': BASE}])
    pg = ctx.new_page()
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.on('console', lambda m: errs.append('console.error: ' + m.text) if m.type == 'error' else None)
    pg.goto(BASE, wait_until='networkidle')
    pg.wait_for_timeout(1500)
    # 直达个股页（茅台）——用 openStock 才会切到个股 tab，否则容器隐藏截不到
    pg.evaluate("openStock('sh600519')")
    # 等 AI 卡出现且有内容（最多 60s，LLM 首次调用较慢）
    ai_txt = ''
    for _ in range(60):
        pg.wait_for_timeout(1000)
        try:
            ai_txt = pg.locator('#stockAiOut').inner_text(timeout=2000)
        except Exception:
            ai_txt = ''
        if ai_txt and '分析中' not in ai_txt and '加载' not in ai_txt and len(ai_txt) > 30:
            break
    # 断言关键元素
    has_plain = '大白话' in ai_txt
    has_flow = '资金面' in ai_txt or '主力净额' in ai_txt
    has_chip = '筹码' in ai_txt or '获利盘' in ai_txt
    has_meta = pg.locator('#stockAiMeta').count() > 0
    plain_chk = pg.locator('#stockAiPlainChk').count() > 0
    refresh_btn = pg.locator('#stockAiRefreshBtn').count() > 0
    card = pg.locator('#stockAiCard')
    png = out_webp[:-5] + '.png'
    card.screenshot(path=png)                       # Locator 截图 → 中转 PNG
    from PIL import Image
    Image.open(png).save(out_webp, 'WEBP', quality=80, method=6)
    os.remove(png)
    open(out_txt, 'w', encoding='utf-8').write(ai_txt)
    print('has_plain(大白话):', has_plain)
    print('has_flow(资金面):', has_flow)
    print('has_chip(筹码):', has_chip)
    print('meta/开关/刷新按钮:', has_meta, plain_chk, refresh_btn)
    print('JS 错误:', errs if errs else '无')
    print('截图:', out_webp)
    assert has_plain and has_flow and has_chip and has_meta and plain_chk and refresh_btn, '关键元素缺失'
    assert not errs, f'JS 报错: {errs}'
    print('OK')
    ctx.close(); b.close()
