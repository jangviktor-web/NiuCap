"""关于页自动化校验。

覆盖：
  1. /api/about 端点：版本、构建时间、指标/策略计数与分组结构齐全自洽
  2. 计数与后端真实引擎一致（61 项 / 29 策略——数字本身会随版本演进，
     这里校验的是「自洽 + 与源数据同源」，不是钉死具体值）
  3. 首页静态 HTML 含动态渲染所需的全部挂载点（aboutBody / ADMIN_TABS）

为什么单开一个文件：关于页的质量问题（数字过期、新页签漏写）历史上都是
「静默漂移」——不出错、只是慢慢变得不对。这里把漂移变成显式断言。

跑法（需先启动服务）：
    python3 tests/check_about.py
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8899"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def get(path, timeout=60):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    # ---- 1. 端点存在且结构齐全 ----
    d = get("/api/about")
    rec("version 存在", bool(d.get("version")), str(d.get("version")))
    rec("built_at 为合理时间戳",
        isinstance(d.get("built_at"), (int, float)) and d["built_at"] > 1600000000,
        str(d.get("built_at")))

    # ---- 2. 指标：自洽（total == 各组之和），并与 indicator_full 同源 ----
    ind = d.get("indicators") or {}
    groups = ind.get("groups") or []
    total = ind.get("total") or 0
    s = sum(g.get("count", 0) for g in groups)
    rec("指标 total == 分组之和", total == s and total > 0,
        f"total={total} sum={s} groups={len(groups)}")
    # 与真实引擎对一次账（indicators_full 用同一套 all_indicators）
    full = get("/api/indicators_full?code=sh600519&count=250")
    rec("与 indicators_full 同源", full.get("indicator_count") == total,
        f"about={total} full={full.get('indicator_count')}")

    # ---- 3. 策略：自洽，并与 strategy_list 同源 ----
    stg = d.get("strategies") or {}
    cats = stg.get("cats") or []
    stotal = stg.get("total") or 0
    ssum = sum(c.get("count", 0) for c in cats)
    rec("策略 total == 分类之和", stotal == ssum and stotal > 0,
        f"total={stotal} sum={ssum} cats={len(cats)}")
    lst = get("/api/strategy_list")
    lst_total = sum(len(c.get("items", [])) for c in lst.get("cats", []))
    rec("与 strategy_list 同源", lst_total == stotal,
        f"about={stotal} list={lst_total}")

    # ---- 4. admin_enabled 是布尔 ----
    rec("admin_enabled 为布尔", isinstance(d.get("admin_enabled"), bool),
        str(d.get("admin_enabled")))

    # ---- 5. 首页含渲染挂载点与 ADMIN_TABS 单一来源 ----
    with urllib.request.urlopen(BASE + "/", timeout=30) as r:
        html = r.read().decode()
    for kw in ['id="aboutBody"', "var ADMIN_TABS", "api('/api/about')",
               "模块构成", "版本与构建"]:
        rec(f"首页含 {kw}", kw in html)

    # ---- 汇总 ----
    ok_n = sum(1 for _, ok, _ in results if ok)
    print(f"\n汇总: {ok_n}/{len(results)} 通过")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    time.sleep(0)
    raise SystemExit(main())
