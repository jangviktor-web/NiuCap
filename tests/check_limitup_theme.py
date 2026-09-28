#!/usr/bin/env python3
# 连板梯队 + 题材雷达（#100）端到端：后端直连校验 + 浏览器渲染 + 现有 tab 不回归
import sys, json, time
import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
SHOT = "shots_limitup_theme"

def main():
    results, fails = [], 0
    def rec(name, ok, detail=""):
        nonlocal fails
        results.append((name, ok, detail))
        if not ok: fails += 1
        print(f"{'✅' if ok else '❌'} {name}  {detail}")

    # ---------- 后端直连校验（不受浏览器时序影响） ----------
    try:
        lu = requests.get(BASE + "/api/limitup?limit=200", timeout=60).json()
        if lu.get("ok") is False:
            rec("连板接口返回结构(含错误)", False, f"{lu}")
        elif not lu.get("trading_day"):
            rec("连板接口·非交易日降级", True, "非交易日，无涨停属正常")
        else:
            n = len(lu.get("涨停", []))
            ld = lu.get("ladder", {})
            tot = sum(len(v) for v in ld.values())
            rec("连板接口·涨停非空", n > 0, f"涨停 {n} 只，梯队合计 {tot}")
            rec("连板接口·梯队分组", set(ld.keys()) >= {"首板","2连板","3连板","4+连板"},
                f"键={list(ld.keys())}")
            rec("连板接口·最高板>0", lu.get("max_height", 0) >= 1, f"最高 {lu.get('max_height')} 板")
            rec("连板接口·情绪周期注入", bool(lu.get("phase")), f"phase={lu.get('phase')}")
    except Exception as e:
        rec("连板接口可达", False, f"{type(e).__name__}: {e}")

    try:
        th = requests.get(BASE + "/api/theme?limit=40", timeout=30).json()
        if th.get("ok") is False:
            rec("题材接口·降级不500", True, f"source_error={th.get('source_error')}")
        else:
            bs = th.get("boards", [])
            rec("题材接口·板块非空", len(bs) > 0, f"{len(bs)} 个板块")
            rec("题材接口·评分有序", all(bs[i]["score"] >= bs[i+1]["score"] for i in range(len(bs)-1)),
                f"首 {bs[0]['name']}={bs[0]['score']}" if bs else "空")
            rec("题材接口·融合去重生效",
                len(bs) == len({b["name"] for b in bs}), "名称无重复")
    except Exception as e:
        rec("题材接口可达", False, f"{type(e).__name__}: {e}")

    # ---------- 浏览器渲染 + 不回归 ----------
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1366, "height": 900})
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(BASE + "/", wait_until="networkidle")
        pg.wait_for_selector(".tabs button", timeout=15000)

        # 非回归：市场榜单（默认 tab）应正常渲染
        pg.wait_for_selector("#idxbar .idx, #rankBody, .card", timeout=15000)
        rec("非回归·市场榜单仍可渲染", True, "")

        # 点击「连板梯队」
        pg.click('.tabs button[data-tab="limitup"]')
        pg.wait_for_selector("#tab-limitup.on", timeout=10000)
        try:
            pg.wait_for_selector("#luBody .lu-item, #luBody .innote, #luBody .lu-tier", timeout=60000)
            has_items = pg.eval_on_selector_all("#luBody .lu-item", "e=>e.length")
            has_tier = pg.eval_on_selector_all("#luBody .lu-tier", "e=>e.length")
            rec("连板梯队·渲染出内容", (has_items + has_tier) > 0 or
                "无涨停" in (pg.text_content("#luBody") or ""),
                f"lu-item={has_items}, lu-tier={has_tier}")
        except Exception as e:
            rec("连板梯队·渲染出内容", False, f"等待超时: {e}")
        # 点击连板项应跳个股页（验证 openStock 接线）
        try:
            if pg.eval_on_selector_all("#luBody .lu-item", "e=>e.length") > 0:
                pg.click("#luBody .lu-item")
                pg.wait_for_selector("#tab-stock.on", timeout=10000)
                rec("连板项→个股分析页跳转", True, "")
                pg.click('.tabs button[data-tab="limitup"]')  # 回连板
                pg.wait_for_selector("#tab-limitup.on", timeout=10000)
        except Exception as e:
            rec("连板项→个股分析页跳转", False, f"{e}")
        pg.screenshot(path=f"{SHOT}/limitup.png", full_page=False)

        # 点击「题材雷达」
        pg.click('.tabs button[data-tab="theme"]')
        pg.wait_for_selector("#tab-theme.on", timeout=10000)
        try:
            pg.wait_for_selector("#thBody .th-row", timeout=30000)
            nrows = pg.eval_on_selector_all("#thBody .th-row", "e=>e.length")
            rec("题材雷达·渲染出板块表", nrows > 1, f"th-row={nrows}")
        except Exception as e:
            rec("题材雷达·渲染出板块表", False, f"等待超时: {e}")
        pg.screenshot(path=f"{SHOT}/theme.png", full_page=False)

        # 非回归：切回市场榜单仍正常
        pg.click('.tabs button[data-tab="rank"]')
        pg.wait_for_selector("#tab-rank.on", timeout=10000)
        rec("非回归·切回市场榜单正常", True, "")

        rec("页面无 JS 报错", not errors, "; ".join(errors[:3])[:200])
        b.close()

    print(f"\n{'通过' if fails==0 else '失败'} {len(results)-fails}/{len(results)}")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
