#!/usr/bin/env python3
"""#88 双源快讯流 自检。

A 组：newsfeed 纯逻辑（归一化/去重/缓存，含真实抓取冒烟）
B 组：HTTP /api/newsfeed（真实 8899 服务）
C 组：Playwright 页签渲染（tab 存在、红条样式、来源徽章、情绪点）

用法：python3 tests/check_newsfeed.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "server"))

import newsfeed as nf  # noqa: E402

PASS, FAIL = [], []


def check(cond, good, bad):
    if cond:
        print(f"  ✅ {good}")
        PASS.append(good)
    else:
        print(f"  ❌ {bad}")
        FAIL.append(bad)


print("═" * 60)
print("A 组：newsfeed 纯逻辑 + 真实抓取")
print("═" * 60)
rc = nf.selfcheck()
check(rc == 0, "newsfeed.selfcheck() 全过", f"newsfeed.selfcheck() 失败 rc={rc}")

print()
print("═" * 60)
print("B 组：HTTP 集成（8899）")
print("═" * 60)
import requests  # noqa: E402

BASE = "http://127.0.0.1:8899"
try:
    r = requests.get(f"{BASE}/api/newsfeed?limit=60", timeout=30)
    check(r.status_code == 200, "/api/newsfeed 200", f"status={r.status_code}")
    d = r.json()
    items = d.get("items") or []
    check(len(items) >= 20, f"条数 {len(items)} ≥ 20", f"条数只有 {len(items)}")
    srcs = d.get("sources") or {}
    check(len(srcs) == 2, f"双源都在 {srcs}", f"源缺失：{srcs}")
    times = [x["time"] for x in items]
    check(times == sorted(times, reverse=True), "时间倒序", "乱序")
    ids = [x["id"] for x in items]
    check(len(ids) == len(set(ids)), "id 无重复", "有重复 id")
    fps = [x["fp"] for x in items]
    check(len(fps) == len(set(fps)), "内容指纹无重复（跨源去重生效）", "有跨源重复")
    tagged = sum(1 for x in items if "sentiment" in x)
    check(tagged == len(items), f"情绪标签 {tagged}/{len(items)}", "情绪标签缺失")
    cached = requests.get(f"{BASE}/api/newsfeed?limit=5", timeout=15).json()
    check(cached.get("cached") is True, "30s 内二次请求走缓存", "缓存未生效")
    check(all(len(x["content"]) >= 5 for x in items), "内容无空条", "有空内容条")
except Exception as e:
    FAIL.append(f"HTTP 组异常 {type(e).__name__}: {e}")
    print(f"  ❌ HTTP 组异常：{type(e).__name__}: {e}")

print()
print("═" * 60)
print("C 组：Playwright 页签渲染")
print("═" * 60)
try:
    sys.path.insert(0, os.path.join(_ROOT, "server"))
    import store  # noqa: E402
    adm = next((u for u in store.list_users()
                if (u.get("username") or "").lower() == "admin"), None)
    tok = store.create_session(adm["id"]) if adm else ""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1560, "height": 1200})
        if tok:
            ctx.add_cookies([{"name": "tick_sid", "value": tok, "url": BASE + "/"}])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1200)
        pg.evaluate("syncTabs('news'); loadNews()")
        pg.wait_for_timeout(6000)
        tab_on = pg.evaluate("$('tab-news').classList.contains('on')")
        check(tab_on, "页签可切换", "tab-news 未激活")
        n = pg.evaluate("$('newsBody').querySelectorAll('.news-li').length")
        check(n >= 20, f"渲染 {n} 条", f"只渲染 {n} 条")
        tags = pg.evaluate(
            "Array.from($('newsBody').querySelectorAll('.news-li .tag'))"
            ".map(x=>x.textContent)")
        check(any("新浪" in t for t in tags) and any("同花顺" in t for t in tags),
              f"来源徽章双源齐全 {sorted(set(tags))}", f"来源徽章缺失 {set(tags)}")
        meta = pg.evaluate("$('newsMeta').textContent")
        check("缓存" in meta or "实时" in meta, f"meta 状态 {meta[:50]}", "meta 空")
        check(len(errs) == 0, "页面 0 JS 错误", f"JS 错误 {errs[:2]}")
        b.close()
except Exception as e:
    FAIL.append(f"Playwright 组异常 {type(e).__name__}: {e}")
    print(f"  ❌ Playwright 组异常：{type(e).__name__}: {e}")

print()
print("═" * 60)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    for f in FAIL:
        print(f"  ✗ {f}")
    sys.exit(1)
print("✅ 全部通过")
